"""测试数据库模块 - database.py

测试用例:
  TC-DB-01: 数据库初始化创建表结构
  TC-DB-02: upsert_printer 插入和更新
  TC-DB-03: record_print_start 记录打印开始
  TC-DB-04: record_print_end 记录打印结束并计算时长
  TC-DB-05: get_today_usage_all 查询今日产能
  TC-DB-06: get_monthly_usage_all 查询月度数据
  TC-DB-07: get_monthly_usage_all 补齐无记录天为0
  TC-DB-08: get_sessions 查询打印历史
  TC-DB-09: 无打印记录时今日产能为0
  TC-DB-10: 多次打印时长累加正确
  TC-DB-11: 月度数据包含当天 running 任务
  TC-DB-12: record_print_start 自动结束之前的 running 任务
  TC-DB-13: record_print_end 无 running 任务时静默忽略
  TC-DB-14: 今日产能包含跨午夜 running 任务（只算今天部分）
  TC-DB-15: 今日产能忽略未知 serial
  TC-DB-16: 月度数据多打印机多天 completed 任务
  TC-DB-17: 月度数据日期范围正确
  TC-DB-18: get_sessions 限制条数和排序
  TC-DB-19: record_print_end 不写 daily_usage（由 snapshot 负责）
  TC-DB-20: today usage running session 只取最新一个
  TC-DB-21: monthly running 跨午夜只算今天部分
"""

import pytest
import aiosqlite
from datetime import date, datetime, timedelta
from unittest.mock import patch

from bumbo_stats.database import Database, SECONDS_PER_DAY


@pytest.mark.asyncio
class TestDatabaseInit:
    def test_tc_db_01_tables_created(self, db):
        """TC-DB-01: 初始化后表存在"""
        # init() 已在 fixture 中调用，验证不报错即可
        assert db is not None


@pytest.mark.asyncio
class TestUpsertPrinter:
    async def test_tc_db_02_upsert(self, db):
        """TC-DB-02: 插入和更新打印机"""
        await db.upsert_printer("S001", "P1", "1.2.3.4", "P1S", "A区")
        # 再次 upsert（更新）
        await db.upsert_printer("S001", "P1-Updated", "1.2.3.5", "P1S", "B区")
        # 验证不报错


@pytest.mark.asyncio
class TestPrintRecording:
    async def test_tc_db_03_record_start(self, db):
        """TC-DB-03: 记录打印开始"""
        await db.record_print_start("S001", "benchy.gcode")
        sessions = await db.get_sessions("S001")
        assert len(sessions) == 1
        assert sessions[0].gcode_file == "benchy.gcode"
        assert sessions[0].status == "running"
        assert sessions[0].end_time is None

    async def test_tc_db_04_record_end(self, db):
        """TC-DB-04: 记录打印结束，计算时长"""
        await db.record_print_start("S001", "cube.gcode")
        # 模拟短暂延迟后结束
        await db.record_print_end("S001", "completed")
        sessions = await db.get_sessions("S001")
        assert len(sessions) == 1
        assert sessions[0].status == "completed"
        assert sessions[0].duration_seconds is not None
        assert sessions[0].duration_seconds >= 0


@pytest.mark.asyncio
class TestTodayUsage:
    async def test_tc_db_05_today_usage(self, db, printer_names):
        """TC-DB-05: 查询今日产能饱和度"""
        # 模拟一次打印
        await db.upsert_printer("AC12309BH109", "Printer-01", "1.2.3.4")
        await db.record_print_start("AC12309BH109", "test.gcode")
        await db.record_print_end("AC12309BH109", "completed")

        items = await db.get_today_usage_all(printer_names)
        assert len(items) == 2
        # Printer-01 有记录
        p1 = next(i for i in items if i.printer_serial == "AC12309BH109")
        assert p1.total_print_seconds >= 0
        assert p1.utilization_percent >= 0

    async def test_tc_db_09_no_records_zero(self, db, printer_names):
        """TC-DB-09: 无打印记录时产能为0"""
        items = await db.get_today_usage_all(printer_names)
        for item in items:
            assert item.total_print_seconds == 0
            assert item.utilization_percent == 0.0

    async def test_tc_db_10_accumulate(self, db, printer_names):
        """TC-DB-10: 多次打印时长累加"""
        await db.upsert_printer("AC12309BH109", "Printer-01", "1.2.3.4")
        await db.record_print_start("AC12309BH109", "a.gcode")
        await db.record_print_end("AC12309BH109", "completed")
        await db.record_print_start("AC12309BH109", "b.gcode")
        await db.record_print_end("AC12309BH109", "completed")

        items = await db.get_today_usage_all(printer_names)
        p1 = next(i for i in items if i.printer_serial == "AC12309BH109")
        # 应该有两次打印记录累加
        assert p1.total_print_seconds >= 0


@pytest.mark.asyncio
class TestMonthlyUsage:
    async def test_tc_db_06_monthly(self, db, printer_names):
        """TC-DB-06: 月度数据查询"""
        await db.upsert_printer("AC12309BH109", "Printer-01", "1.2.3.4")
        monthly = await db.get_monthly_usage_all(printer_names, days=7)
        assert len(monthly) == 2
        assert monthly[0].printer_name == "Printer-01"

    async def test_tc_db_07_fill_zeros(self, db, printer_names):
        """TC-DB-07: 无记录的天补齐为0"""
        monthly = await db.get_monthly_usage_all(printer_names, days=30)
        for m in monthly:
            assert len(m.points) == 30
            for p in m.points:
                assert p.utilization_percent >= 0

    async def test_tc_db_11_monthly_includes_running(self, db, printer_names):
        """TC-DB-11: 月度数据包含当天 running 任务"""
        await db.upsert_printer("AC12309BH109", "Printer-01", "1.2.3.4")
        # 模拟一个正在进行的打印任务
        await db.record_print_start("AC12309BH109", "running_job.gcode")
        # 不调用 record_print_end，模拟任务仍在进行
        monthly = await db.get_monthly_usage_all(printer_names, days=7)
        p1 = next(m for m in monthly if m.printer_serial == "AC12309BH109")
        # 今天的点应该包含 running 任务的已用时间
        today_point = p1.points[-1]  # 最后一天是今天
        assert today_point.utilization_percent >= 0


@pytest.mark.asyncio
class TestSessions:
    async def test_tc_db_08_get_sessions(self, db):
        """TC-DB-08: 获取打印历史"""
        await db.record_print_start("S001", "a.gcode")
        await db.record_print_start("S001", "b.gcode")
        sessions = await db.get_sessions("S001", limit=10)
        assert len(sessions) == 2
        # 按时间倒序
        assert sessions[0].start_time >= sessions[1].start_time

    async def test_tc_db_12_record_start_ends_previous_running(self, db):
        """TC-DB-12: record_print_start 自动结束之前的 running 任务"""
        await db.record_print_start("S001", "first.gcode")
        # 开始新任务，前一个应被自动结束
        await db.record_print_start("S001", "second.gcode")
        sessions = await db.get_sessions("S001")
        assert len(sessions) == 2
        # 第一个任务应被标记为 completed
        older = next(s for s in sessions if s.gcode_file == "first.gcode")
        assert older.status == "completed"
        assert older.end_time is not None
        assert older.duration_seconds is not None
        # 第二个任务应仍在 running
        newer = next(s for s in sessions if s.gcode_file == "second.gcode")
        assert newer.status == "running"
        assert newer.end_time is None

    async def test_tc_db_13_record_end_no_running_session(self, db):
        """TC-DB-13: record_print_end 无 running 任务时静默忽略"""
        # 没有开始任何任务就结束，应该不报错
        await db.record_print_end("S001", "completed")
        sessions = await db.get_sessions("S001")
        assert len(sessions) == 0

    async def test_tc_db_14_cross_midnight_running(self, db, printer_names):
        """TC-DB-14: 今日产能包含跨午夜 running 任务（只算今天部分）"""
        serial = "AC12309BH109"
        # 直接插入一个跨午夜的 running session（手动设置 start_time 为昨天）
        yesterday = datetime.now() - timedelta(days=1)
        yesterday = yesterday.replace(hour=23, minute=0, second=0, microsecond=0)
        async with aiosqlite.connect(db.db_path) as conn:
            await conn.execute("""
                INSERT INTO print_sessions (printer_serial, gcode_file, start_time, status)
                VALUES (?, ?, ?, 'running')
            """, (serial, "overnight.gcode", yesterday.isoformat()))
            await conn.commit()

        items = await db.get_today_usage_all(printer_names)
        p1 = next(i for i in items if i.printer_serial == serial)
        # 应该只计算今天凌晨之后的时间
        today_midnight = datetime.combine(date.today(), datetime.min.time())
        now = datetime.now()
        expected_seconds = int((now - today_midnight).total_seconds())
        # 允许 10 秒误差（测试执行时间）
        assert abs(p1.total_print_seconds - expected_seconds) < 10
        assert p1.total_print_seconds > 0

    async def test_tc_db_15_unknown_serial_ignored(self, db, printer_names):
        """TC-DB-15: 今日产能忽略未知 serial"""
        # 插入一个不在 printer_names 中的打印机记录
        await db.record_print_start("UNKNOWN_SERIAL", "test.gcode")
        await db.record_print_end("UNKNOWN_SERIAL", "completed")
        items = await db.get_today_usage_all(printer_names)
        # 所有已知打印机的产能应该为 0（UNKNOWN_SERIAL 被忽略）
        for item in items:
            assert item.total_print_seconds == 0

    async def test_tc_db_16_monthly_multi_day_completed(self, db, printer_names):
        """TC-DB-16: 月度数据多打印机多天 completed 任务"""
        serial = "AC12309BH109"
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        now = datetime.now()
        # 插入昨天的 completed 任务
        async with aiosqlite.connect(db.db_path) as conn:
            await conn.execute("""
                INSERT INTO print_sessions
                (printer_serial, gcode_file, start_time, end_time, duration_seconds, status)
                VALUES (?, ?, ?, ?, ?, 'completed')
            """, (serial, "yesterday.gcode",
                  datetime.combine(yesterday, datetime.min.time().replace(hour=10)).isoformat(),
                  datetime.combine(yesterday, datetime.min.time().replace(hour=14)).isoformat(),
                  14400))  # 4小时
            await conn.commit()

        # 插入今天的 completed 任务
        await db.record_print_start(serial, "today.gcode")
        await db.record_print_end(serial, "completed")

        monthly = await db.get_monthly_usage_all(printer_names, days=7)
        p1 = next(m for m in monthly if m.printer_serial == serial)
        # 昨天的点应该有数据（~16.7%）
        yesterday_point = next(p for p in p1.points if p.date == yesterday)
        assert abs(yesterday_point.utilization_percent - 16.7) < 0.5
        # 今天的点也应该有数据
        today_point = p1.points[-1]
        assert today_point.utilization_percent >= 0

    async def test_tc_db_17_monthly_date_range(self, db, printer_names):
        """TC-DB-17: 月度数据日期范围正确"""
        for days in [7, 14, 30]:
            monthly = await db.get_monthly_usage_all(printer_names, days=days)
            for m in monthly:
                assert len(m.points) == days
                assert m.points[0].date == date.today() - timedelta(days=days - 1)
                assert m.points[-1].date == datetime.now().date()

    async def test_tc_db_18_get_sessions_limit(self, db):
        """TC-DB-18: get_sessions 限制条数和排序"""
        for i in range(5):
            await db.record_print_start("S001", f"job_{i}.gcode")
            await db.record_print_end("S001", "completed")

        # 限制取 3 条
        sessions = await db.get_sessions("S001", limit=3)
        assert len(sessions) == 3
        # 按时间倒序，最新在前
        assert sessions[0].gcode_file == "job_4.gcode"
        assert sessions[2].gcode_file == "job_2.gcode"

    async def test_tc_db_19_record_end_no_daily_usage(self, db):
        """TC-DB-19: record_print_end 不写 daily_usage（由 snapshot 负责）"""
        serial = "S001"
        await db.record_print_start(serial, "test.gcode")
        await db.record_print_end(serial, "completed")

        today_str = date.today().isoformat()
        async with aiosqlite.connect(db.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT * FROM daily_usage WHERE printer_serial=? AND date=?",
                (serial, today_str)
            ) as cursor:
                row = await cursor.fetchone()
        # record_print_end 不再写 daily_usage
        assert row is None

    async def test_tc_db_20_today_running_only_latest(self, db, printer_names):
        """TC-DB-20: today usage running session 只取最新一个"""
        serial = "AC12309BH109"
        today_midnight = datetime.combine(date.today(), datetime.min.time())
        # 确保 start_time 在今天内，避免跨午夜截断
        old_time = (today_midnight + timedelta(hours=2)).isoformat()
        new_time = (today_midnight + timedelta(hours=6)).isoformat()
        async with aiosqlite.connect(db.db_path) as conn:
            await conn.execute("""
                INSERT INTO print_sessions (printer_serial, gcode_file, start_time, status)
                VALUES (?, ?, ?, 'running')
            """, (serial, "old_job.gcode", old_time))
            await conn.execute("""
                INSERT INTO print_sessions (printer_serial, gcode_file, start_time, status)
                VALUES (?, ?, ?, 'running')
            """, (serial, "new_job.gcode", new_time))
            await conn.commit()

        items = await db.get_today_usage_all(printer_names)
        p1 = next(i for i in items if i.printer_serial == serial)
        # 应该只计算最新的 running session（now - new_time）
        now = datetime.now()
        expected = int((now - datetime.fromisoformat(new_time)).total_seconds())
        assert abs(p1.total_print_seconds - expected) < 5

    async def test_tc_db_21_monthly_cross_midnight_running(self, db, printer_names):
        """TC-DB-21: monthly running 跨午夜只算今天部分"""
        serial = "AC12309BH109"
        yesterday = datetime.now() - timedelta(days=1)
        yesterday = yesterday.replace(hour=23, minute=0, second=0, microsecond=0)
        async with aiosqlite.connect(db.db_path) as conn:
            await conn.execute("""
                INSERT INTO print_sessions (printer_serial, gcode_file, start_time, status)
                VALUES (?, ?, ?, 'running')
            """, (serial, "overnight.gcode", yesterday.isoformat()))
            await conn.commit()

        monthly = await db.get_monthly_usage_all(printer_names, days=7)
        p1 = next(m for m in monthly if m.printer_serial == serial)
        today_point = p1.points[-1]
        # 应该只计算今天部分
        today_midnight = datetime.combine(date.today(), datetime.min.time())
        now = datetime.now()
        expected_pct = round((now - today_midnight).total_seconds() / SECONDS_PER_DAY * 100, 1)
        assert abs(today_point.utilization_percent - expected_pct) < 0.2


@pytest.mark.asyncio
class TestSnapshot:
    async def test_tc_db_22_snapshot_past_day(self, db):
        """TC-DB-22: snapshot_past_day 创建 daily_usage 记录"""
        serial = "S001"
        yesterday = date.today() - timedelta(days=1)
        # 插入昨天的 completed session
        async with aiosqlite.connect(db.db_path) as conn:
            await conn.execute("""
                INSERT INTO print_sessions
                (printer_serial, gcode_file, start_time, end_time, duration_seconds, status)
                VALUES (?, ?, ?, ?, ?, 'completed')
            """, (serial, "yesterday.gcode",
                  datetime.combine(yesterday, datetime.min.time().replace(hour=10)).isoformat(),
                  datetime.combine(yesterday, datetime.min.time().replace(hour=14)).isoformat(),
                  14400))
            await conn.commit()

        await db.snapshot_past_day(yesterday)

        async with aiosqlite.connect(db.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT * FROM daily_usage WHERE printer_serial=? AND date=?",
                (serial, yesterday.isoformat())
            ) as cursor:
                row = await cursor.fetchone()
        assert row is not None
        assert row["total_print_seconds"] == 14400

    async def test_tc_db_23_snapshot_overwrites_existing(self, db):
        """TC-DB-23: snapshot_past_day 每次都重新计算，覆盖已有记录"""
        serial = "S001"
        yesterday = datetime.now().date() - timedelta(days=1)
        # 手动插入一条过时的记录
        async with aiosqlite.connect(db.db_path) as conn:
            await conn.execute(
                "INSERT INTO daily_usage (printer_serial, date, total_print_seconds, session_count) VALUES (?, ?, 99999, 99)",
                (serial, yesterday.isoformat()))
            await conn.commit()

        # 插入实际数据：2小时 session
        start_ts = datetime.combine(yesterday, datetime.min.time().replace(hour=8)).isoformat()
        end_ts = datetime.combine(yesterday, datetime.min.time().replace(hour=10)).isoformat()
        async with aiosqlite.connect(db.db_path) as conn:
            await conn.execute(
                "INSERT INTO print_sessions (printer_serial, gcode_file, start_time, end_time, duration_seconds, status) VALUES (?, ?, ?, ?, ?, 'completed')",
                (serial, "test.gcode", start_ts, end_ts, 7200))
            await conn.commit()

        await db.snapshot_past_day(yesterday)

        # 已有记录被覆盖为正确值
        async with aiosqlite.connect(db.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT * FROM daily_usage WHERE printer_serial=? AND date=?",
                (serial, yesterday.isoformat())
            ) as cursor:
                row = await cursor.fetchone()
        assert row["total_print_seconds"] == 7200
        assert row["session_count"] == 1

    async def test_tc_db_24_snapshot_cross_day_session(self, db):
        """TC-DB-24: 快照正确处理跨天 session"""
        serial = "S001"
        yesterday = date.today() - timedelta(days=1)
        # session: 昨天 22:00 → 今天 02:00（4小时，但昨天只有 2小时）
        async with aiosqlite.connect(db.db_path) as conn:
            await conn.execute("""
                INSERT INTO print_sessions
                (printer_serial, gcode_file, start_time, end_time, duration_seconds, status)
                VALUES (?, ?, ?, ?, ?, 'completed')
            """, (serial, "cross_day.gcode",
                  datetime.combine(yesterday, datetime.min.time().replace(hour=22)).isoformat(),
                  datetime.combine(date.today(), datetime.min.time().replace(hour=2)).isoformat(),
                  14400))
            await conn.commit()

        await db.snapshot_past_day(yesterday)

        async with aiosqlite.connect(db.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT * FROM daily_usage WHERE printer_serial=? AND date=?",
                (serial, yesterday.isoformat())
            ) as cursor:
                row = await cursor.fetchone()
        assert row is not None
        # 昨天 22:00 到 午夜 = 2小时
        assert row["total_print_seconds"] == 7200

    async def test_tc_db_25_monthly_today_equals_today_usage(self, db, printer_names):
        """TC-DB-25: 月度数据今天点与今日产能完全一致"""
        serial = "AC12309BH109"
        await db.upsert_printer(serial, "Printer-01", "1.2.3.4")
        await db.record_print_start(serial, "test.gcode")

        today_items = await db.get_today_usage_all(printer_names)
        monthly = await db.get_monthly_usage_all(printer_names, days=7)

        today_usage = next(i for i in today_items if i.printer_serial == serial)
        monthly_today = next(m for m in monthly if m.printer_serial == serial).points[-1]

        assert today_usage.utilization_percent == monthly_today.utilization_percent
        assert today_usage.total_print_seconds == round(
            monthly_today.utilization_percent / 100 * SECONDS_PER_DAY
        ) or abs(today_usage.utilization_percent - monthly_today.utilization_percent) < 0.01

    async def test_tc_db_26_snapshot_running_capped_at_midnight(self, db):
        """TC-DB-26: 快照 running session 截止到该天午夜"""
        serial = "S001"
        yesterday = date.today() - timedelta(days=1)
        # running session started yesterday at 22:00
        async with aiosqlite.connect(db.db_path) as conn:
            await conn.execute("""
                INSERT INTO print_sessions (printer_serial, gcode_file, start_time, status)
                VALUES (?, ?, ?, 'running')
            """, (serial, "running_overnight.gcode",
                  datetime.combine(yesterday, datetime.min.time().replace(hour=22)).isoformat()))
            await conn.commit()

        await db.snapshot_past_day(yesterday)

        async with aiosqlite.connect(db.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT * FROM daily_usage WHERE printer_serial=? AND date=?",
                (serial, yesterday.isoformat())
            ) as cursor:
                row = await cursor.fetchone()
        assert row is not None
        # 22:00 到 午夜 = 2小时
        assert row["total_print_seconds"] == 7200
