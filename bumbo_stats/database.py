"""数据库模块 - SQLite 初始化与操作"""

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import aiosqlite

from .models import DailyUsage, MonthlyUsage, MonthlyUsagePoint, PrintSession, TodayUsageItem

logger = logging.getLogger(__name__)

SECONDS_PER_DAY = 86400


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

    async def record_print_start(self, serial: str, gcode_file: str):
        """记录打印任务开始，先结束该打印机所有已有的 running 任务"""
        now = datetime.now()
        async with aiosqlite.connect(self.db_path) as db:
            # 结束该打印机所有已有的 running 任务（防止状态闪烁产生重复记录）
            async with db.execute("""
                SELECT id, start_time FROM print_sessions
                WHERE printer_serial=? AND status='running'
            """, (serial,)) as cursor:
                old_sessions = await cursor.fetchall()

            for row in old_sessions:
                old_id, old_start_str = row
                old_start = datetime.fromisoformat(old_start_str)
                duration = int((now - old_start).total_seconds())
                await db.execute("""
                    UPDATE print_sessions
                    SET end_time=?, duration_seconds=?, status='completed'
                    WHERE id=?
                """, (now.isoformat(), duration, old_id))

            # 创建新的 running 任务
            await db.execute("""
                INSERT INTO print_sessions (printer_serial, gcode_file, start_time, status)
                VALUES (?, ?, ?, 'running')
            """, (serial, gcode_file, now.isoformat()))
            await db.commit()
        logger.info("记录打印开始: %s - %s", serial, gcode_file)

    async def record_print_end(self, serial: str, status: str):
        """记录打印任务结束，计算时长并更新 print_sessions

        注意：不写 daily_usage。今日产能由 get_today_usage_all 从 sessions 实时计算，
        历史数据由 snapshot_past_day 在每天午夜后一次性快照固化。
        """
        now = datetime.now()

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
                duration = int((now - start_time).total_seconds())

                await db.execute("""
                    UPDATE print_sessions
                    SET end_time=?, duration_seconds=?, status=?
                    WHERE id=?
                """, (now.isoformat(), duration, status, session_id))
                await db.commit()
                logger.info("记录打印结束: %s 时长=%ds 状态=%s", serial, duration, status)

    async def snapshot_past_day(self, target_date: date):
        """快照指定日期的产能数据到 daily_usage

        计算逻辑与 get_today_usage_all 保持一致：
        - completed: duration_seconds 中属于该天的部分
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

        # printer_totals[serial] = (total_seconds, session_count)
        printer_totals: dict[str, tuple[int, int]] = {}
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
                # 只计算属于 target_date 的时间段
                effective_start = max(start_time, target_midnight)
                effective_end = min(end_time, next_day_midnight)
                if effective_start < effective_end:
                    day_seconds = int((effective_end - effective_start).total_seconds())
                    # 使用 min(duration, day_boundary) 确保不超报
                    prev = printer_totals.get(serial, (0, 0))
                    printer_totals[serial] = (prev[0] + min(dur, day_seconds), prev[1] + 1)

        # running 任务：截止到 min(now, 该天午夜)，防止跨午夜 session 被算满整天
        running_cutoff = min(now, next_day_midnight)
        for serial, start_time in latest_running.items():
            effective_start = max(start_time, target_midnight)
            if effective_start < running_cutoff:
                duration = int((running_cutoff - effective_start).total_seconds())
                prev = printer_totals.get(serial, (0, 0))
                printer_totals[serial] = (prev[0] + duration, prev[1] + 1)

        # 写入 daily_usage（先删后插，每次快照都重新计算完整数据）
        async with aiosqlite.connect(self.db_path) as db:
            for serial, (total, count) in printer_totals.items():
                if total > 0:
                    await db.execute(
                        "DELETE FROM daily_usage WHERE printer_serial=? AND date=?",
                        (serial, target_str),
                    )
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

        # 按打印机分组统计
        printer_totals: dict[str, int] = {s: 0 for s in printer_names}
        # 跟踪每台打印机最新的 running session
        latest_running: dict[str, datetime] = {}

        for session in sessions:
            serial = session["printer_serial"]
            if serial not in printer_totals:
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
                start_time = datetime.fromisoformat(session["start_time"])
                end_time = datetime.fromisoformat(session["end_time"])
                effective_start = max(start_time, today_midnight)
                effective_end = min(end_time, today_midnight + timedelta(days=1))
                if effective_start < effective_end:
                    day_seconds = int((effective_end - effective_start).total_seconds())
                    printer_totals[serial] += min(dur, day_seconds)

        # 加上每台打印机最新 running 任务的时长（跨午夜任务只算今天部分）
        for serial, start_time in latest_running.items():
            if serial in printer_totals:
                effective_start = max(start_time, today_midnight)
                duration = int((now - effective_start).total_seconds())
                printer_totals[serial] += duration

        result = []
        for serial, name in printer_names.items():
            total_seconds = printer_totals.get(serial, 0)
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
