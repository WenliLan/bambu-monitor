"""数据库模块 - SQLite 初始化与操作"""

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import aiosqlite

from .models import MonthlyUsage, MonthlyUsagePoint, PrintSession, TodayUsageItem
from .printer.notify_common import normalize_gcode

logger = logging.getLogger(__name__)

SECONDS_PER_DAY = 86400


def _union_seconds(intervals: list[tuple[datetime, datetime]],
                   window_start: datetime, window_end: datetime) -> int:
    """区间裁剪到窗口后求并集长度（秒）

    逐条累加会把重叠的 session 重复计入，导致单日产能超过 100%。
    并集天然不超过窗口长度，无需再封顶。
    """
    clipped = sorted((max(s, window_start), min(e, window_end)) for s, e in intervals)
    total = 0
    seg_start = seg_end = None
    for s, e in clipped:
        if s >= e:
            continue
        if seg_end is None:
            seg_start, seg_end = s, e
        elif s > seg_end:
            total += int((seg_end - seg_start).total_seconds())
            seg_start, seg_end = s, e
        elif e > seg_end:
            seg_end = e
    if seg_end is not None:
        total += int((seg_end - seg_start).total_seconds())
    return total


class Database:
    """SQLite 异步数据库操作"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_dir()

    def _ensure_dir(self):
        """确保数据库目录存在"""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    async def init(self):
        """创建表结构和索引"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS printers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    serial TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    model TEXT,
                    location TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS print_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    printer_serial TEXT NOT NULL,
                    gcode_file TEXT,
                    start_time TIMESTAMP NOT NULL,
                    end_time TIMESTAMP,
                    duration_seconds INTEGER,
                    status TEXT DEFAULT 'running',
                    FOREIGN KEY (printer_serial) REFERENCES printers(serial)
                );

                CREATE TABLE IF NOT EXISTS daily_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    printer_serial TEXT NOT NULL,
                    date DATE NOT NULL,
                    total_print_seconds INTEGER DEFAULT 0,
                    session_count INTEGER DEFAULT 0,
                    UNIQUE(printer_serial, date),
                    FOREIGN KEY (printer_serial) REFERENCES printers(serial)
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_serial
                    ON print_sessions(printer_serial);
                CREATE INDEX IF NOT EXISTS idx_sessions_start
                    ON print_sessions(start_time);
                CREATE INDEX IF NOT EXISTS idx_usage_date
                    ON daily_usage(date);
                CREATE INDEX IF NOT EXISTS idx_usage_serial_date
                    ON daily_usage(printer_serial, date);
            """)
            await db.commit()
        logger.info("数据库初始化完成: %s", self.db_path)

    async def upsert_printer(self, serial: str, name: str, ip: str,
                             model: str = None, location: str = None):
        """插入或更新打印机记录"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO printers (serial, name, ip, model, location, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(serial) DO UPDATE SET
                    name=excluded.name, ip=excluded.ip,
                    model=excluded.model, location=excluded.location,
                    updated_at=CURRENT_TIMESTAMP
            """, (serial, name, ip, model, location))
            await db.commit()

    async def update_heartbeat(self):
        """更新服务心跳时间（最后存活时刻）

        服务运行期间定期调用。重启后结算停机期间遗留的孤儿 session 时，
        用心跳时间作为结算时刻封顶，避免把整个停机期误算为打印时长。
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO meta (key, value) VALUES ('last_active', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (datetime.now().isoformat(),),
            )
            await db.commit()

    async def get_last_heartbeat(self) -> datetime | None:
        """读取上次运行的心跳时间（服务最后存活时刻），无记录返回 None"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT value FROM meta WHERE key='last_active'"
            ) as cursor:
                row = await cursor.fetchone()
        if not row or not row[0]:
            return None
        try:
            return datetime.fromisoformat(row[0])
        except ValueError:
            return None

    async def record_print_start(self, serial: str, gcode_file: str,
                                 settle_time: datetime | None = None,
                                 start_time: datetime | None = None):
        """记录打印任务开始，先结束该打印机所有已有的 running 任务

        同一任务（gcode 归一化后相同）已在 running 时幂等返回，不重复建记录。

        Args:
            settle_time: 启动补录场景传入的心跳时刻，作为遗留 running 任务的结算上限
                （停机期没有观测证据，不能当作打印时长）。
            start_time: 云端任务记录给出的真实开始时刻。提供时新任务一律从该时刻起算，
                不再用心跳时刻倒填——倒填会把当前任务名安到几十小时前的时刻上，
                凭空造出跨停机期的连续打印。
        """
        now = datetime.now()
        async with aiosqlite.connect(self.db_path) as db:
            # 结束该打印机所有已有的 running 任务（防止状态闪烁产生重复记录）
            async with db.execute("""
                SELECT id, start_time, gcode_file FROM print_sessions
                WHERE printer_serial=? AND status='running'
            """, (serial,)) as cursor:
                old_sessions = await cursor.fetchall()

            # 同一任务重复上报时直接返回：启动时 MQTT 首次状态与云端补录会各调一次，
            # 第二次若继续走结算+新建，会把刚建的 session 压成 0 秒，并按更早的
            # 心跳时刻倒填出一条与之重叠的 running，产能因此翻倍。
            # gcode 任一侧为空视为同一任务（没有证据表明换了任务）。
            new_name = normalize_gcode(gcode_file)
            for _, _, old_gcode in old_sessions:
                old_name = normalize_gcode(old_gcode)
                if not new_name or not old_name or new_name == old_name:
                    return

            # 新任务起算时刻：云端真实开始时刻 > 心跳（仅在有遗留任务时）> 当前时间
            if start_time is not None:
                new_start = start_time
            elif old_sessions and settle_time:
                new_start = settle_time
            else:
                new_start = now
            # 遗留任务的结算时刻不得晚于新任务开始，否则两条 session 会重叠
            ref_time = min(settle_time or now, new_start)
            if old_sessions and ref_time != now:
                logger.info("启动补录结算 %s: 结算到 %s", serial, ref_time.isoformat())

            for row in old_sessions:
                old_id, old_start_str, _ = row
                old_start = datetime.fromisoformat(old_start_str)
                # 心跳时刻早于旧任务开始时（异常数据）封顶为 0 时长
                effective_ref = max(ref_time, old_start)
                duration = int((effective_ref - old_start).total_seconds())
                await db.execute("""
                    UPDATE print_sessions
                    SET end_time=?, duration_seconds=?, status='completed'
                    WHERE id=?
                """, (effective_ref.isoformat(), duration, old_id))

            # 创建新的 running 任务
            await db.execute("""
                INSERT INTO print_sessions (printer_serial, gcode_file, start_time, status)
                VALUES (?, ?, ?, 'running')
            """, (serial, gcode_file, new_start.isoformat()))
            await db.commit()
        logger.info("记录打印开始: %s - %s", serial, gcode_file)

    async def record_print_end(self, serial: str, status: str,
                               end_time: datetime | None = None):
        """记录打印任务结束，计算时长并更新 print_sessions

        注意：不写 daily_usage。今日产能由 get_today_usage_all 从 sessions 实时计算，
        历史数据由 snapshot_past_day 在每天午夜后一次性快照固化。

        Args:
            end_time: 显式结算时刻（启动时用上次心跳封顶，避免停机期被算成打印时长）；
                默认当前时间。
        """
        now = end_time if end_time is not None else datetime.now()

        async with aiosqlite.connect(self.db_path) as db:
            # 查找该打印机最近的 running 任务
            async with db.execute("""
                SELECT id, start_time FROM print_sessions
                WHERE printer_serial=? AND status='running'
                ORDER BY start_time DESC LIMIT 1
            """, (serial,)) as cursor:
                row = await cursor.fetchone()

            if row:
                session_id, start_time_str = row
                start_time = datetime.fromisoformat(start_time_str)
                # 结算时刻早于开始时刻（异常数据）时封顶为 0 时长
                effective_end = max(now, start_time)
                duration = int((effective_end - start_time).total_seconds())

                await db.execute("""
                    UPDATE print_sessions
                    SET end_time=?, duration_seconds=?, status=?
                    WHERE id=?
                """, (effective_end.isoformat(), duration, status, session_id))
                await db.commit()
                logger.info("记录打印结束: %s 时长=%ds 状态=%s", serial, duration, status)

    async def snapshot_past_day(self, target_date: date):
        """快照指定日期的产能数据到 daily_usage

        计算逻辑与 get_today_usage_all 保持一致：
        - completed: 取属于该天的时间区间，重叠部分求并集只算一次
        - running: 截止到 min(now, 该天午夜) 的时间，防止跨午夜 session 被算满 24h
        """
        target_str = target_date.isoformat()
        next_day = target_date + timedelta(days=1)
        next_day_midnight = datetime.combine(next_day, datetime.min.time())
        target_midnight = datetime.combine(target_date, datetime.min.time())
        now = datetime.now()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # 该天开始的 session + 该天结束的 session(含跨午夜) + 任何 running
            async with db.execute("""
                SELECT printer_serial, start_time, end_time, duration_seconds, status
                FROM print_sessions
                WHERE date(start_time) = ?
                   OR (end_time IS NOT NULL AND date(end_time) >= ?)
                   OR (status = 'running' AND date(start_time) < ?)
                ORDER BY start_time
            """, (target_str, target_str, next_day.isoformat())) as cursor:
                sessions = await cursor.fetchall()

        # printer_intervals[serial] = 落在该天的时间区间，counts[serial] = 贡献时长的任务数
        printer_intervals: dict[str, list[tuple[datetime, datetime]]] = {}
        counts: dict[str, int] = {}
        latest_running: dict[str, datetime] = {}

        for s in sessions:
            serial = s["printer_serial"]
            start_time = datetime.fromisoformat(s["start_time"])

            if s["status"] == "running" or not s["end_time"]:
                # running: 取最新的（按 start_time 排序，后面的覆盖前面的）
                latest_running[serial] = start_time
            else:
                # 跳过 duration_seconds=0 的废弃 session（旧版测试数据）
                dur = s["duration_seconds"]
                if dur is None or dur <= 0:
                    continue
                end_time = datetime.fromisoformat(s["end_time"])
                # 只保留与该天有交集的区间，裁剪交给 _union_seconds
                if max(start_time, target_midnight) < min(end_time, next_day_midnight):
                    printer_intervals.setdefault(serial, []).append((start_time, end_time))
                    counts[serial] = counts.get(serial, 0) + 1

        # running 任务：截止到 min(now, 该天午夜)，防止跨午夜 session 被算满整天
        running_cutoff = min(now, next_day_midnight)
        for serial, start_time in latest_running.items():
            if max(start_time, target_midnight) < running_cutoff:
                printer_intervals.setdefault(serial, []).append((start_time, running_cutoff))
                counts[serial] = counts.get(serial, 0) + 1

        printer_totals = {
            serial: (
                _union_seconds(iv, target_midnight, next_day_midnight),
                counts.get(serial, 0),
            )
            for serial, iv in printer_intervals.items()
        }

        # 写入 daily_usage（先删后插，每次快照都重新计算完整数据）
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM daily_usage WHERE date=?", (target_str,))
            for serial, (total, count) in printer_totals.items():
                if total > 0:
                    await db.execute(
                        "INSERT INTO daily_usage (printer_serial, date, total_print_seconds, session_count) VALUES (?, ?, ?, ?)",
                        (serial, target_str, total, count),
                    )
            await db.commit()
        logger.info("快照 %s: %s", target_str,
                     {s[-6:]: f"{v[0]}s({v[1]}次)" for s, v in printer_totals.items()})

    async def snapshot_yesterday(self):
        """快照昨天的产能数据（每天仅执行一次，已存在则跳过）"""
        yesterday = datetime.now().date() - timedelta(days=1)
        await self.snapshot_past_day(yesterday)

    async def get_today_usage_all(self, printer_names: dict[str, str] = None) -> list[TodayUsageItem]:
        """查询所有打印机今日产能饱和度（基于 print_sessions 实时计算）"""
        today_str = datetime.now().date().isoformat()
        now = datetime.now()
        today_midnight = datetime.combine(datetime.now().date(), datetime.min.time())
        printer_names = printer_names or {}

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # 当天开始的任务 + 当天结束的任务(含跨午夜) + 任何 running 任务
            async with db.execute("""
                SELECT printer_serial, start_time, end_time, duration_seconds, status
                FROM print_sessions
                WHERE date(start_time) = ?
                   OR (end_time IS NOT NULL AND date(end_time) >= ?)
                   OR (end_time IS NULL)
                ORDER BY start_time
            """, (today_str, today_str)) as cursor:
                sessions = await cursor.fetchall()

        # 按打印机收集时间区间，重叠部分交给 _union_seconds 去重
        printer_intervals: dict[str, list[tuple[datetime, datetime]]] = {
            s: [] for s in printer_names
        }
        # 跟踪每台打印机最新的 running session
        latest_running: dict[str, datetime] = {}

        for session in sessions:
            serial = session["printer_serial"]
            if serial not in printer_intervals:
                continue

            if session["status"] == "running" or not session["end_time"]:
                # running 任务：只保留最新的一个（防止重复计算）
                start_time = datetime.fromisoformat(session["start_time"])
                # 更新为最新的 running 起始时间
                latest_running[serial] = start_time
            else:
                # 已完成任务：只计算属于今天的部分（处理跨午夜 session）
                # 跳过 duration_seconds=0 的废弃 session
                dur = session["duration_seconds"]
                if dur is None or dur <= 0:
                    continue
                printer_intervals[serial].append((
                    datetime.fromisoformat(session["start_time"]),
                    datetime.fromisoformat(session["end_time"]),
                ))

        # 加上每台打印机最新 running 任务的时长（跨午夜任务只算今天部分）
        for serial, start_time in latest_running.items():
            printer_intervals[serial].append((start_time, now))

        result = []
        for serial, name in printer_names.items():
            total_seconds = _union_seconds(
                printer_intervals.get(serial, []),
                today_midnight, today_midnight + timedelta(days=1),
            )
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            result.append(TodayUsageItem(
                printer_serial=serial,
                printer_name=name,
                total_print_seconds=total_seconds,
                utilization_percent=round(total_seconds / SECONDS_PER_DAY * 100, 1),
                print_time_display=f"{hours}h {minutes:02d}m",
            ))
        return result

    async def get_monthly_usage_all(self, printer_names: dict[str, str],
                                    days: int = 30) -> list[MonthlyUsage]:
        """查询所有打印机近N天每日产能数据（用于折线图）

        历史数据从 daily_usage 表读取（快照固化），
        今天数据直接复用 get_today_usage_all() 结果，保证与今日产能卡片完全一致。
        """
        today_str = datetime.now().date().isoformat()
        start_date = (datetime.now().date() - timedelta(days=days - 1)).isoformat()

        # 确保昨天已快照（已存在则跳过）
        await self.snapshot_yesterday()

        # 今天的数据直接复用 get_today_usage_all
        today_items = await self.get_today_usage_all(printer_names)
        today_seconds: dict[str, int] = {
            item.printer_serial: item.total_print_seconds for item in today_items
        }

        # 历史数据从 daily_usage 读取
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT printer_serial, date, total_print_seconds
                FROM daily_usage
                WHERE date >= ? AND date < ?
            """, (start_date, today_str)) as cursor:
                rows = await cursor.fetchall()

        data_map: dict[str, dict[str, int]] = {}
        for row in rows:
            serial = row["printer_serial"]
            data_map.setdefault(serial, {})
            data_map[serial][row["date"]] = row["total_print_seconds"]

        # 今天的数据覆盖
        for serial, secs in today_seconds.items():
            data_map.setdefault(serial, {})
            data_map[serial][today_str] = secs

        # 生成完整日期序列
        all_dates = [(datetime.now().date() - timedelta(days=i)).isoformat()
                     for i in range(days - 1, -1, -1)]

        result = []
        for serial, name in printer_names.items():
            serial_data = data_map.get(serial, {})
            points = [
                MonthlyUsagePoint(
                    date=date.fromisoformat(d),
                    utilization_percent=round(
                        serial_data.get(d, 0) / SECONDS_PER_DAY * 100, 1
                    ),
                )
                for d in all_dates
            ]
            result.append(MonthlyUsage(
                printer_serial=serial,
                printer_name=name,
                points=points,
            ))
        return result

    async def get_sessions(self, serial: str, limit: int = 20) -> list[PrintSession]:
        """获取打印任务历史"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT id, printer_serial, gcode_file, start_time,
                       end_time, duration_seconds, status
                FROM print_sessions
                WHERE printer_serial=?
                ORDER BY start_time DESC
                LIMIT ?
            """, (serial, limit)) as cursor:
                rows = await cursor.fetchall()

        return [
            PrintSession(
                id=r["id"],
                printer_serial=r["printer_serial"],
                gcode_file=r["gcode_file"],
                start_time=datetime.fromisoformat(r["start_time"]),
                end_time=datetime.fromisoformat(r["end_time"]) if r["end_time"] else None,
                duration_seconds=r["duration_seconds"],
                status=r["status"],
            )
            for r in rows
        ]
