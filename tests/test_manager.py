"""测试打印机管理器 - printer/manager.py

测试用例:
  TC-MGR-01: 初始化配置映射
  TC-MGR-02: serials 和 printer_names 属性
  TC-MGR-03: get_status 未连接时返回 OFFLINE
  TC-MGR-04: get_all_statuses 包含所有配置的打印机
  TC-MGR-05: _parse_status 解析云端 MQTT 数据（含 subtask_name）
  TC-MGR-07: add_printer 动态添加后立即可在 serials/printer_names/get_all_statuses 中查到
  TC-MGR-08: remove_printer 动态移除后立即从 serials/printer_names/get_all_statuses 中消失
  TC-MGR-09: 重复 add_printer 不会重复创建
  TC-MGR-11: _parse_status 解析 Bambu 实际 hms[] (attr + code) 得到 attr/raw_code/severity
  TC-MGR-12: _parse_status 从 code>>16 推出 severity (fatal/serious/common)
  TC-MGR-13: _parse_status 兼容老字段 err_code/severity/err_msg
"""

import pytest

from bambu_monitor.config import PrinterConfig
from bambu_monitor.models import PrinterState


@pytest.fixture
def configs():
    return [
        PrinterConfig(name="P1", serial="S001", ip="1.2.3.4", access_code="0000"),
        PrinterConfig(name="P2", serial="S002", ip="1.2.3.5", access_code="0001"),
    ]


class TestPrinterManagerInit:
    def test_tc_mgr_01_init(self, configs):
        """TC-MGR-01: 初始化配置映射"""
        from bambu_monitor.printer.manager import PrinterManager
        mgr = PrinterManager(configs)
        assert mgr.get_config("S001").name == "P1"
        assert mgr.get_config("S002").name == "P2"
        assert mgr.get_config("NONEXIST") is None

    def test_tc_mgr_02_properties(self, configs):
        """TC-MGR-02: serials 和 printer_names"""
        from bambu_monitor.printer.manager import PrinterManager
        mgr = PrinterManager(configs)
        assert set(mgr.serials) == {"S001", "S002"}
        assert mgr.printer_names == {"S001": "P1", "S002": "P2"}


class TestPrinterManagerStatus:
    def test_tc_mgr_03_offline_status(self, configs):
        """TC-MGR-03: 未连接的打印机返回 OFFLINE（从缓存）"""
        from bambu_monitor.printer.manager import PrinterManager
        mgr = PrinterManager(configs)
        # 不调用 connect_all，直接获取状态
        status = mgr.get_status("S001")
        assert status.state == PrinterState.OFFLINE
        assert status.serial == "S001"

    def test_tc_mgr_04_get_all_statuses_includes_offline(self, configs):
        """TC-MGR-04: get_all_statuses 包含所有配置的打印机"""
        from bambu_monitor.printer.manager import PrinterManager
        mgr = PrinterManager(configs)
        results = mgr.get_all_statuses()
        assert "S001" in results
        assert "S002" in results
        assert all(s.state == PrinterState.OFFLINE for s in results.values())

    def test_tc_mgr_05_parse_status_cloud_mqtt(self, configs):
        """TC-MGR-05: _parse_status 正确解析云端 MQTT 数据（含 subtask_name）"""
        from bambu_monitor.printer.manager import PrinterManager
        mgr = PrinterManager(configs)
        data = {
            "gcode_state": "RUNNING",
            "nozzle_temper": 220.5,
            "nozzle_target_temper": 220.0,
            "bed_temper": 60.0,
            "bed_target_temper": 60.0,
            "chamber_temper": 35.0,
            "mc_percent": 45,
            "mc_remaining_time": 32,
            "layer_num": 50,
            "total_layer_num": 120,
            "subtask_name": "benchy_3dbenchy.gcode",  # 云端字段
            "spd_lvl": 2,
            "wifi_signal": "-45dBm",
            "lights_report": [{"mode": "on"}],
        }
        status = mgr._parse_status("S001", "P1", data)
        assert status.state == PrinterState.RUNNING
        assert status.nozzle_temp == 220.5
        assert status.progress == 45
        assert status.remaining_time == 32
        # 验证 subtask_name 映射到 gcode_file
        assert status.gcode_file == "benchy_3dbenchy.gcode"
        assert status.light_on is True

    def test_tc_mgr_06_parse_status_gcode_file(self, configs):
        """TC-MGR-06: _parse_status 同时支持 gcode_file 字段"""
        from bambu_monitor.printer.manager import PrinterManager
        mgr = PrinterManager(configs)
        data = {
            "gcode_state": "IDLE",
            "gcode_file": "dragon.gcode",  # 本地字段
        }
        status = mgr._parse_status("S002", "P2", data)
        assert status.gcode_file == "dragon.gcode"


class TestHMSFieldParsing:
    """Bambu 实际 MQTT `hms[]` 字段解析。

    实际字段 (参考 Bambuddy Go 代码):
      attr : 4 byte, [Module ID 8][Module No 8][Part ID 8][Part No 8]
             用于查 HMS_ERROR_CODES 注册表中文名
      code : 4 byte, [Alert level 16][Error code ID 16]
             高 16 位 = 告警严重级别 (1=fatal / 2=serious / 3=common)
             低 16 位 = 错误码 ID
    """

    def test_tc_mgr_11_parse_hms_attr_and_code(self, configs):
        """TC-MGR-11: _parse_status 解析 attr+code 得到 attr/raw_code/severity"""
        from bambu_monitor.printer.manager import PrinterManager
        mgr = PrinterManager(configs)
        # 实际案例: 右挤出机喷嘴温控异常
        #   attr = 0x03000200  -> 注册表中 0x03000200
        #   code = 0x00010200  -> 高 16 = 1 (fatal), 低 16 = 0x0200 (错误码 ID)
        data = {
            "gcode_state": "RUNNING",
            "hms": [
                {"attr": 0x03000200, "code": 0x00010200},
            ],
        }
        status = mgr._parse_status("S001", "P1", data)
        assert len(status.hms_errors) == 1
        err = status.hms_errors[0]
        assert err.attr == 0x03000200
        assert err.raw_code == 0x00010200
        # code 字段 (即 attr) 用于查注册表
        assert err.code == 0x03000200
        # severity 从 code>>16 推
        assert err.severity == "fatal"

    def test_tc_mgr_12_severity_from_code_high16(self, configs):
        """TC-MGR-12: code 高 16 位 -> severity (fatal/serious/common)"""
        from bambu_monitor.printer.manager import PrinterManager
        mgr = PrinterManager(configs)
        cases = [
            # (attr, code, expected_severity, expected_short_code)
            (0x03000200, 0x00010200, "fatal",   "0300_0200"),
            (0x03000200, 0x00020200, "serious", "0300_0200"),
            (0x03000200, 0x00030200, "common",  "0300_0200"),
            (0x07FE2000, 0x00020300, "serious", "07FE_0300"),
            (0x0C000200, 0x0001000A, "fatal",   "0C00_000A"),
        ]
        for attr, code, sev, short in cases:
            data = {"gcode_state": "IDLE", "hms": [{"attr": attr, "code": code}]}
            status = mgr._parse_status("S001", "P1", data)
            err = status.hms_errors[0]
            assert err.severity == sev, (
                f"attr=0x{attr:08X} code=0x{code:08X} "
                f"expected severity={sev}, got {err.severity}"
            )
            # 短码格式: (attr >> 16) 拼接 (raw_code & 0xFFFF)
            computed_short = (
                f"{(err.attr >> 16) & 0xFFFF:04X}_{err.raw_code & 0xFFFF:04X}"
            )
            assert computed_short == short

    def test_tc_mgr_13_compat_legacy_fields(self, configs):
        """TC-MGR-13: 兼容老字段 err_code / severity / err_msg"""
        from bambu_monitor.printer.manager import PrinterManager
        mgr = PrinterManager(configs)
        data = {
            "gcode_state": "RUNNING",
            "hms": [
                {
                    "err_code": 0x03000200,    # 老字段名
                    "severity": "error",       # 老字段名 (字符串)
                    "err_msg": "legacy message",
                },
            ],
        }
        status = mgr._parse_status("S001", "P1", data)
        err = status.hms_errors[0]
        assert err.code == 0x03000200
        assert err.attr == 0x03000200
        assert err.severity == "error"
        assert err.message == "legacy message"

    def test_tc_mgr_14_hms_empty_and_invalid(self, configs):
        """TC-MGR-14: 空 hms / 非 list / 非 dict 均不报错"""
        from bambu_monitor.printer.manager import PrinterManager
        mgr = PrinterManager(configs)
        # 空
        s1 = mgr._parse_status("S001", "P1", {"gcode_state": "IDLE", "hms": []})
        assert s1.hms_errors == []
        # None
        s2 = mgr._parse_status("S001", "P1", {"gcode_state": "IDLE", "hms": None})
        assert s2.hms_errors == []
        # 非 list
        s3 = mgr._parse_status("S001", "P1", {"gcode_state": "IDLE", "hms": "bad"})
        assert s3.hms_errors == []
        # 含非 dict 项
        s4 = mgr._parse_status(
            "S001", "P1",
            {"gcode_state": "IDLE", "hms": [{"attr": 0x03000200, "code": 0x00010001}, "bad", None]},
        )
        assert len(s4.hms_errors) == 1
        assert s4.hms_errors[0].attr == 0x03000200
        assert s4.hms_errors[0].severity == "fatal"


class TestPrinterManagerDynamicReload:
    """运行时动态增删打印机的回归测试。

    背景：管理后台“保存打印机”会调用 _reload_printers_impl，
    该函数使用 await manager.add_printer() / await manager.remove_printer()。
    如果漏写 await，协程从未运行，_configs 不会更新，
    导致 /api/printers、/api/usage/today、/api/usage/monthly
    都拿不到新打印机的数据，表现为面板上“打印机状态/产能/趋势”均为空。
    """

    async def test_tc_mgr_07_add_printer_visible_immediately(self, configs):
        """TC-MGR-07: add_printer 后立刻可见（serials / printer_names / get_all_statuses）"""
        from bambu_monitor.printer.manager import PrinterManager
        mgr = PrinterManager(configs)
        new_printer = PrinterConfig(
            name="NewP", serial="S003", ip="1.2.3.6", access_code="0002",
            location="新位置",
        )

        # 修复前：不 await 会导致下面断言全部失败
        await mgr.add_printer(new_printer)

        assert "S003" in mgr.serials
        assert mgr.printer_names.get("S003") == "NewP"
        all_statuses = mgr.get_all_statuses()
        assert "S003" in all_statuses
        # 未连接 MQTT，状态应为 OFFLINE（与现有 TC-MGR-03 行为一致）
        assert all_statuses["S003"].state == PrinterState.OFFLINE

    async def test_tc_mgr_08_remove_printer_visible_immediately(self, configs):
        """TC-MGR-08: remove_printer 后立刻不可见"""
        from bambu_monitor.printer.manager import PrinterManager
        mgr = PrinterManager(configs)

        await mgr.remove_printer("S001")

        assert "S001" not in mgr.serials
        assert "S001" not in mgr.printer_names
        all_statuses = mgr.get_all_statuses()
        assert "S001" not in all_statuses
        # 其余打印机不受影响
        assert "S002" in mgr.serials
        assert "S002" in all_statuses

    async def test_tc_mgr_09_add_printer_idempotent(self, configs):
        """TC-MGR-09: 重复 add_printer 不会覆盖已有配置（仅跳过）"""
        from bambu_monitor.printer.manager import PrinterManager
        mgr = PrinterManager(configs)
        duplicate = PrinterConfig(
            name="DifferentName", serial="S001", ip="9.9.9.9", access_code="9999",
        )

        await mgr.add_printer(duplicate)

        # serial 已存在时不应覆盖原配置
        existing = mgr.get_config("S001")
        assert existing.name == "P1"
        assert existing.ip == "1.2.3.4"
        assert existing.access_code == "0000"

    async def test_tc_mgr_10_reload_simulation(self, configs):
        """TC-MGR-10: 复现 _reload_printers_impl 核心逻辑（await 必须存在）

        场景：原来只有 S001，保存后变为 S001 + S002，
        管理面板调用 reload_printers 之后，S002 必须出现在 manager 中。
        修复前：add_printer 未 await，S002 不会出现在 _configs 中。
        """
        from bambu_monitor.printer.manager import PrinterManager
        mgr = PrinterManager([configs[0]])  # 只有 S001

        # 模拟 _reload_printers_impl 核心循环（修复后用 await）
        new_printers = list(configs)
        old_serials = set(mgr.serials)
        new_serials = {p.serial for p in new_printers}
        for serial in old_serials - new_serials:
            await mgr.remove_printer(serial)
        for printer in new_printers:
            if printer.serial in new_serials - old_serials:
                await mgr.add_printer(printer)

        # 验证：新增的 S002 在所有接口中都能查到
        assert "S002" in mgr.serials
        assert "S002" in mgr.printer_names
        assert "S002" in mgr.get_all_statuses()