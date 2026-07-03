"""测试打印机管理器 - printer/manager.py

测试用例:
  TC-MGR-01: 初始化配置映射
  TC-MGR-02: serials 和 printer_names 属性
  TC-MGR-03: get_status 未连接时返回 OFFLINE
  TC-MGR-04: get_all_statuses 包含所有配置的打印机
  TC-MGR-05: _parse_status 解析云端 MQTT 数据（含 subtask_name）
"""

import pytest
from unittest.mock import MagicMock
from datetime import datetime

from bumbo_stats.config import PrinterConfig
from bumbo_stats.models import PrinterState


@pytest.fixture
def configs():
    return [
        PrinterConfig(name="P1", serial="S001", ip="1.2.3.4", access_code="0000"),
        PrinterConfig(name="P2", serial="S002", ip="1.2.3.5", access_code="0001"),
    ]


class TestPrinterManagerInit:
    def test_tc_mgr_01_init(self, configs):
        """TC-MGR-01: 初始化配置映射"""
        from bumbo_stats.printer.manager import PrinterManager
        mgr = PrinterManager(configs)
        assert mgr.get_config("S001").name == "P1"
        assert mgr.get_config("S002").name == "P2"
        assert mgr.get_config("NONEXIST") is None

    def test_tc_mgr_02_properties(self, configs):
        """TC-MGR-02: serials 和 printer_names"""
        from bumbo_stats.printer.manager import PrinterManager
        mgr = PrinterManager(configs)
        assert set(mgr.serials) == {"S001", "S002"}
        assert mgr.printer_names == {"S001": "P1", "S002": "P2"}


class TestPrinterManagerStatus:
    def test_tc_mgr_03_offline_status(self, configs):
        """TC-MGR-03: 未连接的打印机返回 OFFLINE（从缓存）"""
        from bumbo_stats.printer.manager import PrinterManager
        mgr = PrinterManager(configs)
        # 不调用 connect_all，直接获取状态
        status = mgr.get_status("S001")
        assert status.state == PrinterState.OFFLINE
        assert status.serial == "S001"

    def test_tc_mgr_04_get_all_statuses_includes_offline(self, configs):
        """TC-MGR-04: get_all_statuses 包含所有配置的打印机"""
        from bumbo_stats.printer.manager import PrinterManager
        mgr = PrinterManager(configs)
        results = mgr.get_all_statuses()
        assert "S001" in results
        assert "S002" in results
        assert all(s.state == PrinterState.OFFLINE for s in results.values())

    def test_tc_mgr_05_parse_status_cloud_mqtt(self, configs):
        """TC-MGR-05: _parse_status 正确解析云端 MQTT 数据（含 subtask_name）"""
        from bumbo_stats.printer.manager import PrinterManager
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
        from bumbo_stats.printer.manager import PrinterManager
        mgr = PrinterManager(configs)
        data = {
            "gcode_state": "IDLE",
            "gcode_file": "dragon.gcode",  # 本地字段
        }
        status = mgr._parse_status("S002", "P2", data)
        assert status.gcode_file == "dragon.gcode"