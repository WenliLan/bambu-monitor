"""精确数值验证测试 - 覆盖产能统计全链路计算

所有测试使用固定时间 (14:30:00) 和动态日期 (date.today())，
消除时间不确定性，确保每个数值都被精确验证。

Mock 策略: MagicMock(spec=datetime) 替换 database.datetime，
  now() → today 14:30:00, today() → date.today()

覆盖场景:
  TC-NV-01~04: 已完成任务时长精确计算
  TC-NV-05~08: 运行中任务时长精确计算（含跨午夜、仅最新）
  TC-NV-09~10: 利用率百分比精确计算
  TC-NV-11~12: 显示字符串精确格式化
  TC-NV-13~14: 开始新任务自动结束旧任务精确时长
  TC-NV-15~16: 结束任务更新 daily_usage 精确累加
  TC-NV-17~20: 快照数值精确验证（completed/running/跨天/幂等）
  TC-NV-21~23: 月度数据精确验证（全零/混合/多打印机）
  TC-NV-24~28: 边界条件（跨午夜/未知serial/无数据/日期序列）
"""

import pytest
import aiosqlite
from datetime import date, datetime, timedelta
from unittest.mock import patch, MagicMock

from bumbo_stats.database import Database, SECONDS_PER_DAY


# ===== 动态日期 (相对于真实今天) =====
TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)
TODAY_STR = TODAY.isoformat()
YESTERDAY_STR = YESTERDAY.isoformat()

# 固定时钟: 14:30:00
FIXED_HOUR, FIXED_MIN, FIXED_SEC = 14, 30, 0
FIXED_NOW = datetime.combine(TODAY, datetime.min.time().replace(
    hour=FIXED_HOUR, minute=FIXED_MIN, second=FIXED_SEC))
FIXED_NOW_ISO = FIXED_NOW.isoformat()

# 固定时间字符串辅助
def _today_at(h, m=0, s=0):
    return datetime.combine(TODAY, datetime.min.time().replace(hour=h, minute=m, second=s)).isoformat()

def _yesterday_at(h, m=0, s=0):
    return datetime.combine(YESTERDAY, datetime.min.time().replace(hour=h, minute=m, second=s)).isoformat()


# ===== Mock 工厂 =====

_real_datetime = datetime  # 保存原始 datetime 引用


def _make_mock_dt():
    """创建 datetime mock:
    - now() → FIXED_NOW (固定时间)
    - today() → TODAY (固定日期)
    - fromisoformat/combine/min/max → 委托给真实 datetime
    """
    m = MagicMock(spec=datetime)
    m.now.return_value = FIXED_NOW
    m.today.return_value = TODAY
    # 需要返回真实 datetime 的方法（代码中用 max/min/比较/算术）
    m.fromisoformat.side_effect = _real_datetime.fromisoformat
    m.combine.side_effect = _real_datetime.combine
    m.min = _real_datetime.min
    m.max = _real_datetime.max
    return m


def _patch_dt():
    """返回 patch 上下文管理器"""
    return patch("bumbo_stats.database.datetime", _make_mock_dt())


# ===== 辅助函数 =====

async def _insert_completed(db, serial, gcode, start_str, end_str, duration):
    async with aiosqlite.connect(db.db_path) as conn:
        await conn.execute("""
            INSERT INTO print_sessions
            (printer_serial, gcode_file, start_time, end_time, duration_seconds, status)
            VALUES (?, ?, ?, ?, ?, 'completed')
        """, (serial, gcode, start_str, end_str, duration))
        await conn.commit()


async def _insert_running(db, serial, gcode, start_str):
    async with aiosqlite.connect(db.db_path) as conn:
        await conn.execute("""
            INSERT INTO print_sessions
            (printer_serial, gcode_file, start_time, status)
            VALUES (?, ?, ?, 'running')
        """, (serial, gcode, start_str))
        await conn.commit()


async def _insert_daily_usage(db, serial, date_str, seconds, count=1):
    async with aiosqlite.connect(db.db_path) as conn:
        await conn.execute("""
            INSERT INTO daily_usage
            (printer_serial, date, total_print_seconds, session_count)
            VALUES (?, ?, ?, ?)
        """, (serial, date_str, seconds, count))
        await conn.commit()


async def _read_daily_usage(db, serial, date_str):
    async with aiosqlite.connect(db.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM daily_usage WHERE printer_serial=? AND date=?",
            (serial, date_str)
        ) as cursor:
            return await cursor.fetchone()


# ===== TC-NV-01~04: 已完成任务时长精确计算 =====

@pytest.mark.asyncio
class TestCompletedDurationExact:
    """精确验证 completed 任务的 duration_seconds 累加"""

    async def test_tc_nv_01_single_completed(self, db, printer_names):
        """TC-NV-01: 单个 completed — 3h30m = 12600s → 14.6%"""
        serial = "AC12309BH109"
        await _insert_completed(
            db, serial, "a.gcode",
            _today_at(8, 0), _today_at(11, 30), 12600,
        )
        with _patch_dt():
            items = await db.get_today_usage_all(printer_names)
        p = next(i for i in items if i.printer_serial == serial)
        assert p.total_print_seconds == 12600
        assert p.utilization_percent == round(12600 / SECONDS_PER_DAY * 100, 1)
        assert p.print_time_display == "3h 30m"

    async def test_tc_nv_02_two_completed(self, db, printer_names):
        """TC-NV-02: 两个 completed — 4h + 3h = 25200s → 29.2%"""
        serial = "AC12309BH109"
        await _insert_completed(
            db, serial, "a.gcode",
            _today_at(8, 0), _today_at(12, 0), 14400,
        )
        await _insert_completed(
            db, serial, "b.gcode",
            _today_at(12, 30), _today_at(15, 30), 10800,
        )
        with _patch_dt():
            items = await db.get_today_usage_all(printer_names)
        p = next(i for i in items if i.printer_serial == serial)
        assert p.total_print_seconds == 25200
        assert p.utilization_percent == round(25200 / SECONDS_PER_DAY * 100, 1)
        assert p.print_time_display == "7h 00m"

    async def test_tc_nv_03_three_completed(self, db, printer_names):
        """TC-NV-03: 三个 completed — 2h + 3h + 1h = 21600s → 25.0%"""
        serial = "AC12309BH109"
        await _insert_completed(
            db, serial, "a.gcode",
            _today_at(6, 0), _today_at(8, 0), 7200,
        )
        await _insert_completed(
            db, serial, "b.gcode",
            _today_at(9, 0), _today_at(12, 0), 10800,
        )
        await _insert_completed(
            db, serial, "c.gcode",
            _today_at(12, 30), _today_at(13, 30), 3600,
        )
        with _patch_dt():
            items = await db.get_today_usage_all(printer_names)
        p = next(i for i in items if i.printer_serial == serial)
        assert p.total_print_seconds == 21600
        assert p.utilization_percent == 25.0
        assert p.print_time_display == "6h 00m"

    async def test_tc_nv_04_completed_cross_midnight_today_portion(self, db, printer_names):
        """TC-NV-04: completed 跨午夜 — 只算今天部分 (00:00→02:00 = 7200s)"""
        serial = "AC12309BH109"
        # start 昨天 23:00, end 今天 02:00, duration=10800 (3h)
        # 今天部分: max(start, midnight)=00:00, min(end, next_midnight)=02:00 → 7200s
        await _insert_completed(
            db, serial, "overnight.gcode",
            _yesterday_at(23, 0), _today_at(2, 0), 10800,
        )
        with _patch_dt():
            items = await db.get_today_usage_all(printer_names)
        p = next(i for i in items if i.printer_serial == serial)
        assert p.total_print_seconds == 7200
        assert p.utilization_percent == round(7200 / SECONDS_PER_DAY * 100, 1)


# ===== TC-NV-05~08: 运行中任务时长精确计算 =====

@pytest.mark.asyncio
class TestRunningDurationExact:
    """精确验证 running 任务的时长计算（now - effective_start）"""

    async def test_tc_nv_05_running_basic(self, db, printer_names):
        """TC-NV-05: running start 12:00, now 14:30 → 9000s (2h30m)"""
        serial = "AC12309BH109"
        await _insert_running(db, serial, "r.gcode", _today_at(12, 0))
        with _patch_dt():
            items = await db.get_today_usage_all(printer_names)
        p = next(i for i in items if i.printer_serial == serial)
        # 14:30 - 12:00 = 2.5h = 9000s
        assert p.total_print_seconds == 9000
        assert p.utilization_percent == round(9000 / SECONDS_PER_DAY * 100, 1)
        assert p.print_time_display == "2h 30m"

    async def test_tc_nv_06_running_cross_midnight(self, db, printer_names):
        """TC-NV-06: running 跨午夜 — start 昨天 22:00, 只算 今天 00:00→14:30 → 52200s"""
        serial = "AC12309BH109"
        await _insert_running(db, serial, "overnight.gcode", _yesterday_at(22, 0))
        with _patch_dt():
            items = await db.get_today_usage_all(printer_names)
        p = next(i for i in items if i.printer_serial == serial)
        # effective_start = max(昨天22:00, 今天00:00) = 今天00:00
        # duration = 14:30 - 00:00 = 14.5h = 52200s
        assert p.total_print_seconds == 52200
        assert p.utilization_percent == round(52200 / SECONDS_PER_DAY * 100, 1)
        assert p.print_time_display == "14h 30m"

    async def test_tc_nv_07_completed_plus_running(self, db, printer_names):
        """TC-NV-07: 2 completed + 1 running — 3h + 1h + 2.5h = 23400s (6h30m)"""
        serial = "AC12309BH109"
        await _insert_completed(
            db, serial, "a.gcode",
            _today_at(6, 0), _today_at(9, 0), 10800,  # 3h
        )
        await _insert_completed(
            db, serial, "b.gcode",
            _today_at(9, 30), _today_at(10, 30), 3600,  # 1h
        )
        await _insert_running(db, serial, "c.gcode", _today_at(12, 0))
        with _patch_dt():
            items = await db.get_today_usage_all(printer_names)
        p = next(i for i in items if i.printer_serial == serial)
        # 10800 + 3600 + (14:30-12:00=9000) = 23400
        assert p.total_print_seconds == 23400
        assert p.utilization_percent == round(23400 / SECONDS_PER_DAY * 100, 1)
        assert p.print_time_display == "6h 30m"

    async def test_tc_nv_08_two_running_only_latest(self, db, printer_names):
        """TC-NV-08: 2 running 只取最新 — 旧的忽略"""
        serial = "AC12309BH109"
        await _insert_running(db, serial, "old.gcode", _today_at(6, 0))
        await _insert_running(db, serial, "new.gcode", _today_at(12, 0))
        with _patch_dt():
            items = await db.get_today_usage_all(printer_names)
        p = next(i for i in items if i.printer_serial == serial)
        # 只取 new: 14:30 - 12:00 = 9000s
        assert p.total_print_seconds == 9000
        assert p.utilization_percent == round(9000 / SECONDS_PER_DAY * 100, 1)


# ===== TC-NV-09~10: 利用率百分比精确计算 =====

@pytest.mark.asyncio
class TestUtilizationPercentExact:
    """精确验证 round(seconds / 86400 * 100, 1) 公式"""

    async def test_tc_nv_09_exact_hours(self, db, printer_names):
        """TC-NV-09: 整小时精确百分比"""
        serial = "AC12309BH109"
        # 用 (start_hour, end_hour) 确保时间戳与 duration 一致
        cases = [
            (6, 12, 21600, 25.0),   # 6h
            (0, 12, 43200, 50.0),   # 12h
            (8, 9, 3600, 4.2),      # 1h
            (8, 10, 7200, 8.3),     # 2h
            (8, 11, 10800, 12.5),   # 3h
        ]
        for start_h, end_h, seconds, expected_pct in cases:
            async with aiosqlite.connect(db.db_path) as conn:
                await conn.execute("DELETE FROM print_sessions")
                await conn.commit()
            await _insert_completed(
                db, serial, "test.gcode",
                _today_at(start_h, 0), _today_at(end_h, 0), seconds,
            )
            with _patch_dt():
                items = await db.get_today_usage_all(printer_names)
            p = next(i for i in items if i.printer_serial == serial)
            assert p.total_print_seconds == seconds
            assert p.utilization_percent == expected_pct, (
                f"{seconds}s → {expected_pct}%, got {p.utilization_percent}"
            )

    async def test_tc_nv_10_percentage_precision(self, db, printer_names):
        """TC-NV-10: 非整除百分比 — 5h13m = 18780s → 21.7%"""
        serial = "AC12309BH109"
        await _insert_completed(
            db, serial, "test.gcode",
            _today_at(8, 0), _today_at(13, 13), 18780,
        )
        with _patch_dt():
            items = await db.get_today_usage_all(printer_names)
        p = next(i for i in items if i.printer_serial == serial)
        assert p.total_print_seconds == 18780
        assert p.utilization_percent == 21.7


# ===== TC-NV-11~12: 显示字符串精确格式化 =====

@pytest.mark.asyncio
class TestDisplayStringExact:
    """精确验证 '{h}h {mm:02d}m' 格式"""

    async def test_tc_nv_11_display_format(self, db, printer_names):
        """TC-NV-11: 显示格式 — 含前导零、0h、大小时"""
        serial = "AC12309BH109"
        # 用 (start, end) 确保时间戳与 duration 一致
        cases = [
            ("0h 00m", None),                          # 0s = no session
            ("0h 00m", (_today_at(8, 0), _today_at(8, 0, 30), 30)),
            ("0h 01m", (_today_at(8, 0), _today_at(8, 1), 60)),
            ("1h 00m", (_today_at(8, 0), _today_at(9, 0), 3600)),
            ("1h 01m", (_today_at(8, 0), _today_at(9, 1), 3660)),
            ("10h 00m", (_today_at(0, 0), _today_at(10, 0), 36000)),
            ("18h 00m", (_today_at(0, 0), _today_at(18, 0), 64800)),
            ("23h 59m", (_today_at(0, 0), _today_at(23, 59), 86340)),
        ]
        for expected_display, session_data in cases:
            async with aiosqlite.connect(db.db_path) as conn:
                await conn.execute("DELETE FROM print_sessions")
                await conn.commit()
            if session_data:
                start, end, seconds = session_data
                await _insert_completed(
                    db, serial, "test.gcode", start, end, seconds,
                )
            with _patch_dt():
                items = await db.get_today_usage_all(printer_names)
            p = next(i for i in items if i.printer_serial == serial)
            if session_data:
                assert p.total_print_seconds == session_data[2]
            assert p.print_time_display == expected_display, (
                f"got '{p.print_time_display}', expected '{expected_display}'"
            )

    async def test_tc_nv_12_all_zero_display(self, db, printer_names):
        """TC-NV-12: 无记录 → 全零显示"""
        with _patch_dt():
            items = await db.get_today_usage_all(printer_names)
        for item in items:
            assert item.total_print_seconds == 0
            assert item.utilization_percent == 0.0
            assert item.print_time_display == "0h 00m"


# ===== TC-NV-13~14: 开始新任务自动结束旧任务 =====

@pytest.mark.asyncio
class TestRecordPrintStartEndsPrevious:
    """精确验证 record_print_start 自动结束旧 running"""

    async def test_tc_nv_13_start_ends_previous_exact(self, db):
        """TC-NV-13: 旧任务时长 = now - old_start = 23400s (6h30m)"""
        serial = "S001"
        await _insert_running(db, serial, "old.gcode", _today_at(8, 0))
        with _patch_dt():
            await db.record_print_start(serial, "new.gcode")

        sessions = await db.get_sessions(serial)
        assert len(sessions) == 2
        old = next(s for s in sessions if s.gcode_file == "old.gcode")
        assert old.status == "completed"
        # 14:30 - 08:00 = 6.5h = 23400s
        assert old.duration_seconds == 23400
        new = next(s for s in sessions if s.gcode_file == "new.gcode")
        assert new.status == "running"

    async def test_tc_nv_14_start_ends_multiple_previous(self, db):
        """TC-NV-14: 多个旧 running 全部被结束"""
        serial = "S001"
        await _insert_running(db, serial, "old1.gcode", _today_at(6, 0))
        await _insert_running(db, serial, "old2.gcode", _today_at(8, 0))
        with _patch_dt():
            await db.record_print_start(serial, "new.gcode")

        sessions = await db.get_sessions(serial)
        assert len(sessions) == 3
        old1 = next(s for s in sessions if s.gcode_file == "old1.gcode")
        old2 = next(s for s in sessions if s.gcode_file == "old2.gcode")
        assert old1.duration_seconds == 30600   # 14:30 - 06:00 = 8.5h
        assert old2.duration_seconds == 23400   # 14:30 - 08:00 = 6.5h
        assert old1.status == "completed"
        assert old2.status == "completed"


# ===== TC-NV-15~16: 结束任务不写 daily_usage =====

@pytest.mark.asyncio
class TestRecordPrintEndNoDailyUsage:
    """精确验证 record_print_end 不写 daily_usage（由 snapshot 负责）"""

    async def test_tc_nv_15_end_no_daily_usage(self, db, printer_names):
        """TC-NV-15: 结束任务后 daily_usage 无记录，今日产能从 sessions 实时算"""
        serial = "AC12309BH109"
        await _insert_running(db, serial, "test.gcode", _today_at(10, 0))
        with _patch_dt():
            await db.record_print_end(serial, "completed")
        # daily_usage 无记录
        row = await _read_daily_usage(db, serial, TODAY_STR)
        assert row is None
        # 今日产能从 sessions 实时计算正确
        with _patch_dt():
            items = await db.get_today_usage_all(printer_names)
        p = next(i for i in items if i.printer_serial == serial)
        # 14:30 - 10:00 = 4.5h = 16200s
        assert p.total_print_seconds == 16200

    async def test_tc_nv_16_two_ends_no_daily_usage(self, db, printer_names):
        """TC-NV-16: 两次结束后 daily_usage 无记录，今日产能累加正确"""
        serial = "AC12309BH109"
        await _insert_running(db, serial, "a.gcode", _today_at(8, 0))
        with _patch_dt():
            await db.record_print_end(serial, "completed")
        await _insert_running(db, serial, "b.gcode", _today_at(12, 0))
        with _patch_dt():
            await db.record_print_end(serial, "completed")
        # daily_usage 无记录
        row = await _read_daily_usage(db, serial, TODAY_STR)
        assert row is None
        # 今日产能从 sessions 实时计算: (14:30-08:00=23400) + (14:30-12:00=9000) = 32400s
        with _patch_dt():
            items = await db.get_today_usage_all(printer_names)
        p = next(i for i in items if i.printer_serial == serial)
        assert p.total_print_seconds == 32400


# ===== TC-NV-17~20: 快照数值精确验证 =====

@pytest.mark.asyncio
class TestSnapshotExact:
    """精确验证 snapshot_past_day 数值"""

    async def test_tc_nv_17_snapshot_completed_exact(self, db):
        """TC-NV-17: completed 快照 — 10:00→14:00 = 14400s (4h)"""
        serial = "S001"
        await _insert_completed(
            db, serial, "test.gcode",
            _yesterday_at(10, 0), _yesterday_at(14, 0), 14400,
        )
        await db.snapshot_past_day(YESTERDAY)
        row = await _read_daily_usage(db, serial, YESTERDAY_STR)
        assert row is not None
        assert row["total_print_seconds"] == 14400

    async def test_tc_nv_18_snapshot_running_capped(self, db):
        """TC-NV-18: running 快照截止午夜 — 22:00→00:00 = 7200s (2h)"""
        serial = "S001"
        await _insert_running(db, serial, "r.gcode", _yesterday_at(22, 0))
        await db.snapshot_past_day(YESTERDAY)
        row = await _read_daily_usage(db, serial, YESTERDAY_STR)
        assert row is not None
        assert row["total_print_seconds"] == 7200

    async def test_tc_nv_19_snapshot_cross_day_session(self, db):
        """TC-NV-19: 跨天 completed — 只取目标天部分 22:00→00:00 = 7200s"""
        serial = "S001"
        await _insert_completed(
            db, serial, "cross.gcode",
            _yesterday_at(22, 0), _today_at(2, 0), 14400,
        )
        await db.snapshot_past_day(YESTERDAY)
        row = await _read_daily_usage(db, serial, YESTERDAY_STR)
        assert row is not None
        assert row["total_print_seconds"] == 7200

    async def test_tc_nv_20_snapshot_overwrites_existing(self, db):
        """TC-NV-20: 快照每次都重新计算，覆盖已有记录"""
        serial = "S001"
        # 先手动写入过时的快照数据
        await _insert_daily_usage(db, serial, YESTERDAY_STR, 99999, 5)
        # 插入 session（快照应覆盖为正确值）
        await _insert_completed(
            db, serial, "test.gcode",
            _yesterday_at(8, 0), _yesterday_at(10, 0), 7200,
        )
        await db.snapshot_past_day(YESTERDAY)
        row = await _read_daily_usage(db, serial, YESTERDAY_STR)
        # 已有数据被覆盖为重新计算后的正确值
        assert row["total_print_seconds"] == 7200
        assert row["session_count"] == 1

    async def test_tc_nv_20b_completed_started_today(self, db):
        """TC-NV-20b: completed 跨天但 start 在目标天 — 截断到午夜 4h"""
        serial = "S001"
        tomorrow = TODAY + timedelta(days=1)
        await _insert_completed(
            db, serial, "late.gcode",
            _today_at(20, 0),
            datetime.combine(tomorrow, datetime.min.time().replace(hour=2)).isoformat(),
            21600,
        )
        await db.snapshot_past_day(TODAY)
        row = await _read_daily_usage(db, serial, TODAY_STR)
        assert row is not None
        # 20:00 → midnight = 4h = 14400s
        assert row["total_print_seconds"] == 14400

    async def test_tc_nv_20c_snapshot_mixed(self, db):
        """TC-NV-20c: 快照 mixed completed + running — 2h + 4h = 21600s"""
        serial = "S001"
        await _insert_completed(
            db, serial, "a.gcode",
            _yesterday_at(10, 0), _yesterday_at(12, 0), 7200,  # 2h
        )
        await _insert_running(db, serial, "b.gcode", _yesterday_at(20, 0))
        await db.snapshot_past_day(YESTERDAY)
        row = await _read_daily_usage(db, serial, YESTERDAY_STR)
        assert row is not None
        # completed: 7200 + running capped: 20:00→00:00=14400 = 21600 (6h)
        assert row["total_print_seconds"] == 21600


# ===== TC-NV-21~23: 月度数据精确验证 =====

@pytest.mark.asyncio
class TestMonthlyExact:
    """精确验证 get_monthly_usage_all"""

    async def test_tc_nv_21_monthly_all_zeros(self, db, printer_names):
        """TC-NV-21: 无记录 — 全零"""
        with _patch_dt():
            monthly = await db.get_monthly_usage_all(printer_names, days=7)
        for m in monthly:
            assert len(m.points) == 7
            for p in m.points:
                assert p.utilization_percent == 0.0

    async def test_tc_nv_22_monthly_history_plus_today(self, db, printer_names):
        """TC-NV-22: 历史快照 + 今日 running"""
        serial = "AC12309BH109"
        # 昨天 session: 10:00→14:00 = 14400s → 16.7%
        await _insert_completed(
            db, serial, "yest.gcode",
            _yesterday_at(10, 0), _yesterday_at(14, 0), 14400,
        )
        # 今天 running: 12:00 → 14:30 = 9000s → 10.4%
        await _insert_running(db, serial, "today.gcode", _today_at(12, 0))

        with _patch_dt():
            monthly = await db.get_monthly_usage_all(printer_names, days=7)

        m = next(x for x in monthly if x.printer_serial == serial)
        assert len(m.points) == 7
        # 前5天: 0.0%
        for i in range(5):
            assert m.points[i].utilization_percent == 0.0
        # 昨天 (index 5): 16.7%
        assert m.points[5].utilization_percent == 16.7
        # 今天 (index 6): 10.4%
        assert m.points[6].utilization_percent == round(9000 / SECONDS_PER_DAY * 100, 1)

    async def test_tc_nv_23_monthly_multi_printer(self, db, printer_names):
        """TC-NV-23: 多打印机独立计算"""
        s1, s2 = "AC12309BH109", "AC12309BH110"
        # 用 session 而非 daily_usage，因为 snapshot 会从 session 重算
        await _insert_completed(
            db, s1, "a.gcode",
            _yesterday_at(10, 0), _yesterday_at(14, 0), 14400,  # 4h → 16.7%
        )
        await _insert_completed(
            db, s2, "b.gcode",
            _yesterday_at(6, 0), _yesterday_at(14, 0), 28800,  # 8h → 33.3%
        )

        with _patch_dt():
            monthly = await db.get_monthly_usage_all(printer_names, days=7)

        m1 = next(m for m in monthly if m.printer_serial == s1)
        m2 = next(m for m in monthly if m.printer_serial == s2)
        assert m1.points[5].utilization_percent == 16.7
        assert m2.points[5].utilization_percent == 33.3
        assert m1.points[6].utilization_percent == 0.0
        assert m2.points[6].utilization_percent == 0.0

    async def test_tc_nv_23b_monthly_today_equals_today_usage(self, db, printer_names):
        """TC-NV-23b: 月度今天点严格等于 get_today_usage_all"""
        serial = "AC12309BH109"
        await _insert_completed(
            db, serial, "a.gcode",
            _today_at(8, 0), _today_at(11, 0), 10800,
        )
        await _insert_running(db, serial, "b.gcode", _today_at(12, 0))

        with _patch_dt():
            today_items = await db.get_today_usage_all(printer_names)
            monthly = await db.get_monthly_usage_all(printer_names, days=7)

        today_item = next(i for i in today_items if i.printer_serial == serial)
        monthly_today = next(m for m in monthly if m.printer_serial == serial).points[-1]
        assert today_item.utilization_percent == monthly_today.utilization_percent


# ===== TC-NV-24~28: 边界条件 =====

@pytest.mark.asyncio
class TestEdgeCaseExact:
    """精确验证边界场景"""

    async def test_tc_nv_24_end_no_running(self, db):
        """TC-NV-24: 无 running 时结束 — 无副作用"""
        with _patch_dt():
            await db.record_print_end("S001", "completed")
        assert len(await db.get_sessions("S001")) == 0
        assert await _read_daily_usage(db, "S001", TODAY_STR) is None

    async def test_tc_nv_25_unknown_serial_excluded(self, db, printer_names):
        """TC-NV-25: 未知 serial 不计入结果"""
        serial_known = "AC12309BH109"
        await _insert_completed(
            db, serial_known, "a.gcode",
            _today_at(10, 0), _today_at(12, 0), 7200,
        )
        await _insert_completed(
            db, "UNKNOWN_999", "x.gcode",
            _today_at(8, 0), _today_at(13, 0), 18000,
        )
        with _patch_dt():
            items = await db.get_today_usage_all(printer_names)
        p_known = next(i for i in items if i.printer_serial == serial_known)
        assert p_known.total_print_seconds == 7200
        assert all(i.printer_serial != "UNKNOWN_999" for i in items)

    async def test_tc_nv_26_completed_before_today(self, db, printer_names):
        """TC-NV-26: completed 昨天之前 → 不计入今日 (end 也在昨天)"""
        serial = "AC12309BH109"
        await _insert_completed(
            db, serial, "old.gcode",
            _yesterday_at(20, 0), _yesterday_at(23, 0), 10800,
        )
        with _patch_dt():
            items = await db.get_today_usage_all(printer_names)
        p = next(i for i in items if i.printer_serial == serial)
        assert p.total_print_seconds == 0

    async def test_tc_nv_27_snapshot_no_sessions(self, db):
        """TC-NV-27: 快照无 session 的日期 — 不创建记录"""
        await db.snapshot_past_day(TODAY - timedelta(days=5))
        row = await _read_daily_usage(db, "S001", (TODAY - timedelta(days=5)).isoformat())
        assert row is None

    async def test_tc_nv_28_monthly_date_sequence(self, db, printer_names):
        """TC-NV-28: 月度日期序列严格连续递增"""
        with _patch_dt():
            monthly = await db.get_monthly_usage_all(printer_names, days=30)
        expected_start = TODAY - timedelta(days=29)
        for m in monthly:
            assert len(m.points) == 30
            assert m.points[0].date == expected_start
            assert m.points[-1].date == TODAY
            for i in range(1, 30):
                assert m.points[i].date == m.points[i - 1].date + timedelta(days=1)
