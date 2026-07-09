"""测试飞书通知模块 - feishu.py

测试用例:
  TC-FEISHU-01: FeishuNotifier 初始化
  TC-FEISHU-02: 未配置时不启动
  TC-FEISHU-03: layer3 事件入队
  TC-FEISHU-04: completed 事件入队
  TC-FEISHU-05: error 事件入队
  TC-FEISHU-06: 同一事件去重（layer3）
  TC-FEISHU-07: 同一事件去重（completed）
  TC-FEISHU-08: 新任务重置 layer3 标记
  TC-FEISHU-09: 新任务重置 completed 标记
  TC-FEISHU-10: HMS 错误合并通知
  TC-FEISHU-11: 同错误不重复通知
  TC-FEISHU-12: 禁用事件不入队
  TC-FEISHU-13: 未知打印机被忽略
  TC-FEISHU-14: 队列满时优雅丢弃
  TC-FEISHU-15: _PrinterNotifState.reset_if_new_job
  TC-FEISHU-16: 消息构建测试
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from bumbo_stats.models import PrinterState, PrinterStatus, HMSError
from bumbo_stats.printer.feishu import (
    FeishuNotifier, FeishuConfig, _PrinterNotifState,
    EVENT_LAYER3, EVENT_COMPLETED, EVENT_ERROR,
)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _make_status(serial, state=PrinterState.RUNNING, gcode_file="test.gcode",
                current_layer=0, total_layers=100, progress=0,
                hms_errors=None):
    return PrinterStatus(
        serial=serial, name="Test", state=state,
        gcode_file=gcode_file, last_updated=datetime.now(),
        current_layer=current_layer, total_layers=total_layers,
        progress=progress,
        hms_errors=hms_errors or [],
    )


def _make_feishu_config():
    return FeishuConfig(
        app_id="fake_app_id",
        app_secret="fake_secret",
        chatid="fake_chatid",
        layer3=True,
        completed=True,
        error=True,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_camera():
    return AsyncMock()


# ---------------------------------------------------------------------------
# TC-FEISHU-01~16: FeishuNotifier 测试
# ---------------------------------------------------------------------------

class TestFeishuNotifier:
    def test_tc_feishu_01_init(self, mock_camera):
        """TC-FEISHU-01: FeishuNotifier 初始化"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)

        assert notifier._cfg.app_id == "fake_app_id"
        assert notifier._cfg.chatid == "fake_chatid"
        assert notifier._tenant_token is None

    def test_tc_feishu_02_no_start_without_config(self):
        """TC-FEISHU-02: 未配置 app_id/app_secret 时不启动"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = FeishuConfig(app_id="", app_secret="")
        notifier = FeishuNotifier(config, [printer_cfg])

        # start() 应该直接返回，不创建后台任务
        async def check():
            await notifier.start()
            assert notifier._task is None
        asyncio.run(check())

    def test_tc_feishu_03_layer3_event_queued(self, mock_camera):
        """TC-FEISHU-03: layer3 事件入队"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        status = _make_status("S001", current_layer=3, total_layers=100, progress=3)
        notifier.notify("S001", EVENT_LAYER3, status)

        assert notifier._queue.qsize() == 1

    def test_tc_feishu_04_completed_event_queued(self, mock_camera):
        """TC-FEISHU-04: completed 事件入队"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        status = _make_status("S001", current_layer=100, total_layers=100, progress=100)
        notifier.notify("S001", EVENT_COMPLETED, status)

        assert notifier._queue.qsize() == 1

    def test_tc_feishu_05_error_event_queued(self, mock_camera):
        """TC-FEISHU-05: error 事件入队"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        errors = [HMSError(code=0x0B01, severity="error", message="堵嘴检测")]
        status = _make_status("S001", hms_errors=errors)
        notifier.notify("S001", EVENT_ERROR, status, errors)

        assert notifier._queue.qsize() == 1

    def test_tc_feishu_06_layer3_deduplicated(self, mock_camera):
        """TC-FEISHU-06: 同一 layer3 事件去重"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        status = _make_status("S001", current_layer=3, total_layers=100, progress=3)

        notifier.notify("S001", EVENT_LAYER3, status)
        assert notifier._queue.qsize() == 1

        notifier.notify("S001", EVENT_LAYER3, status)
        assert notifier._queue.qsize() == 1  # 未增加

    def test_tc_feishu_07_completed_deduplicated(self, mock_camera):
        """TC-FEISHU-07: 同一 completed 事件去重"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        status = _make_status("S001", current_layer=100, total_layers=100, progress=100)

        notifier.notify("S001", EVENT_COMPLETED, status)
        assert notifier._queue.qsize() == 1

        notifier.notify("S001", EVENT_COMPLETED, status)
        assert notifier._queue.qsize() == 1  # 未增加

    def test_tc_feishu_08_new_job_resets_layer3(self, mock_camera):
        """TC-FEISHU-08: 新任务重置 layer3 标记"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        # job1 触发
        status1 = _make_status("S001", gcode_file="job1.gcode", current_layer=3, progress=3)
        notifier.notify("S001", EVENT_LAYER3, status1)
        assert notifier._queue.qsize() == 1

        # job2 触发（新任务）
        status2 = _make_status("S001", gcode_file="job2.gcode", current_layer=3, progress=4)
        notifier.notify("S001", EVENT_LAYER3, status2)
        assert notifier._queue.qsize() == 2

    def test_tc_feishu_09_new_job_resets_completed(self, mock_camera):
        """TC-FEISHU-09: 新任务重置 completed 标记"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        # job1 完成
        status1 = _make_status("S001", gcode_file="job1.gcode", current_layer=100, progress=100)
        notifier.notify("S001", EVENT_COMPLETED, status1)
        assert notifier._queue.qsize() == 1

        # job2 开始后再完成
        status2 = _make_status("S001", gcode_file="job2.gcode", current_layer=100, progress=100)
        notifier.notify("S001", EVENT_COMPLETED, status2)
        assert notifier._queue.qsize() == 2

    def test_tc_feishu_10_hms_errors_merged(self, mock_camera):
        """TC-FEISHU-10: HMS 多个错误合并为一条通知"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        errors = [
            HMSError(code=0x0B01, severity="error", message="堵嘴检测"),
            HMSError(code=0x0501, severity="error", message="缺料"),
        ]
        status = _make_status("S001", hms_errors=errors)
        notifier.notify("S001", EVENT_ERROR, status, errors)

        assert notifier._queue.qsize() == 1
        item = notifier._queue.get_nowait()
        assert len(item["errors"]) == 2

    def test_tc_feishu_11_same_hms_error_not_duplicated(self, mock_camera):
        """TC-FEISHU-11: 同一 HMS 错误不重复通知"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        errors = [HMSError(code=0x0B01, severity="error", message="堵嘴检测")]

        status1 = _make_status("S001", hms_errors=errors)
        notifier.notify("S001", EVENT_ERROR, status1, errors)
        assert notifier._queue.qsize() == 1

        status2 = _make_status("S001", hms_errors=errors)
        notifier.notify("S001", EVENT_ERROR, status2, errors)
        assert notifier._queue.qsize() == 1  # 未增加

    def test_tc_feishu_12_disabled_event_no_queue(self, mock_camera):
        """TC-FEISHU-12: 禁用的通知事件不入队"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        config.layer3 = False  # 禁用 layer3
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        status = _make_status("S001", current_layer=3, total_layers=100, progress=3)
        notifier.notify("S001", EVENT_LAYER3, status)

        assert notifier._queue.qsize() == 0

    def test_tc_feishu_13_unknown_printer_ignored(self, mock_camera):
        """TC-FEISHU-13: 未知打印机被忽略"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        status = _make_status("UNKNOWN", current_layer=3)
        notifier.notify("UNKNOWN", EVENT_LAYER3, status)

        assert notifier._queue.qsize() == 0

    def test_tc_feishu_14_queue_full_drops(self, mock_camera):
        """TC-FEISHU-14: 队列满时优雅丢弃"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=1)  # 队列大小为1

        status = _make_status("S001", current_layer=3)
        notifier.notify("S001", EVENT_LAYER3, status)
        assert notifier._queue.qsize() == 1

        # 队列已满，再次通知应丢弃
        notifier.notify("S001", EVENT_LAYER3, status)
        assert notifier._queue.qsize() == 1  # 仍是1

    def test_tc_feishu_15_printer_notif_state_reset(self):
        """TC-FEISHU-15: _PrinterNotifState.reset_if_new_job"""
        state = _PrinterNotifState()
        state.last_gcode = "job1.gcode"
        state.layer3_sent = True
        state.completed_sent = True

        # 同任务不变
        state.reset_if_new_job("job1.gcode")
        assert state.layer3_sent is True
        assert state.completed_sent is True

        # 新任务重置
        state.reset_if_new_job("job2.gcode")
        assert state.layer3_sent is False
        assert state.completed_sent is False
        assert state.last_gcode == "job2.gcode"

    def test_tc_feishu_16_message_build(self, mock_camera):
        """TC-FEISHU-16: 消息构建测试"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)

        # layer3 消息
        status = _make_status("S001", gcode_file="test.gcode", current_layer=3, total_layers=100, progress=3)
        msg = notifier._build_message("TestPrinter", EVENT_LAYER3, status)
        assert "第 3 层" in msg
        assert "TestPrinter" in msg
        assert "test.gcode" in msg

        # completed 消息
        status = _make_status("S001", gcode_file="test.gcode", current_layer=100, total_layers=100, progress=100)
        msg = notifier._build_message("TestPrinter", EVENT_COMPLETED, status)
        assert "完成" in msg

        # error 消息
        errors = [HMSError(code=0x0B01, severity="error", message="堵嘴检测")]
        msg = notifier._build_message("TestPrinter", EVENT_ERROR, None, errors)
        assert "异常" in msg
        assert "堵嘴检测" in msg
