"""测试数据模型 - models.py

测试用例:
  TC-MDL-01: PrinterState 枚举值完整
  TC-MDL-02: PrinterStatus 默认值
  TC-MDL-03: PrinterStatus 序列化/反序列化
  TC-MDL-04: DailyUsage 利用率计算
  TC-MDL-05: PrintSession 字段验证
  TC-MDL-06: MonthlyUsagePoint/MonthlyUsage 结构
  TC-MDL-07: TodayUsageItem 显示格式
  TC-MDL-08: 模型 model_dump(mode='json') 输出
"""

from datetime import date, datetime

from bumbo_stats.models import (
    PrinterState,
    PrinterStatus,
    DailyUsage,
    PrintSession,
    MonthlyUsagePoint,
    MonthlyUsage,
    TodayUsageItem,
)


class TestPrinterState:
    def test_tc_mdl_01_enum_values(self):
        """TC-MDL-01: PrinterState 包含所有状态"""
        expected = {"IDLE", "RUNNING", "PAUSE", "FINISH", "FAILED", "OFFLINE", "UNKNOWN"}
        actual = {s.value for s in PrinterState}
        assert actual == expected


class TestPrinterStatus:
    def test_tc_mdl_02_defaults(self):
        """TC-MDL-02: PrinterStatus 默认值正确"""
        ps = PrinterStatus(serial="S001", name="P1")
        assert ps.state == PrinterState.UNKNOWN
        assert ps.nozzle_temp == 0.0
        assert ps.progress == 0
        assert ps.gcode_file == ""
        assert ps.light_on is False

    def test_tc_mdl_03_serialization(self):
        """TC-MDL-03: 序列化和反序列化一致"""
        ps = PrinterStatus(
            serial="S001", name="P1", state=PrinterState.RUNNING,
            nozzle_temp=220.5, progress=45, gcode_file="test.gcode",
        )
        data = ps.model_dump()
        ps2 = PrinterStatus(**data)
        assert ps2.serial == "S001"
        assert ps2.state == PrinterState.RUNNING
        assert ps2.nozzle_temp == 220.5

    def test_tc_mdl_08_model_dump_json(self):
        """TC-MDL-08: model_dump(mode='json') 输出可序列化"""
        ps = PrinterStatus(
            serial="S001", name="P1",
            last_updated=datetime(2026, 6, 12, 14, 30, 0),
        )
        data = ps.model_dump(mode="json")
        assert isinstance(data["last_updated"], str)
        assert "2026-06-12" in data["last_updated"]


class TestDailyUsage:
    def test_tc_mdl_04_utilization(self):
        """TC-MDL-04: DailyUsage 利用率百分比"""
        du = DailyUsage(
            printer_serial="S001", date=date.today(),
            total_print_seconds=43200,  # 12小时
            session_count=3,
            utilization_percent=50.0,
        )
        assert du.utilization_percent == 50.0
        assert du.total_print_seconds == 43200


class TestPrintSession:
    def test_tc_mdl_05_fields(self):
        """TC-MDL-05: PrintSession 字段验证"""
        now = datetime.now()
        ps = PrintSession(
            id=1, printer_serial="S001",
            gcode_file="benchy.gcode", start_time=now,
            end_time=None, duration_seconds=None, status="running",
        )
        assert ps.status == "running"
        assert ps.end_time is None


class TestMonthlyUsage:
    def test_tc_mdl_06_structure(self):
        """TC-MDL-06: MonthlyUsage 包含多个数据点"""
        points = [
            MonthlyUsagePoint(date=date(2026, 6, 1), utilization_percent=50.0),
            MonthlyUsagePoint(date=date(2026, 6, 2), utilization_percent=75.0),
        ]
        mu = MonthlyUsage(
            printer_serial="S001", printer_name="P1", points=points,
        )
        assert len(mu.points) == 2
        assert mu.points[1].utilization_percent == 75.0


class TestTodayUsageItem:
    def test_tc_mdl_07_display_format(self):
        """TC-MDL-07: TodayUsageItem 显示格式"""
        item = TodayUsageItem(
            printer_serial="S001", printer_name="P1",
            total_print_seconds=62460,  # 17h 21m
            utilization_percent=72.3,
            print_time_display="17h 21m",
        )
        assert "17h" in item.print_time_display
        assert "21m" in item.print_time_display
