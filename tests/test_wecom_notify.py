"""测试企业微信通知的防重发与防漏发逻辑

测试用例覆盖:
  防重发:
    TC-NOTIFY-01: 同一事件（layer3）多次触发只通知一次
    TC-NOTIFY-02: 同一事件（completed）多次触发只通知一次
    TC-NOTIFY-03: 新 gcode 任务重置 layer3 标记，可再次触发
    TC-NOTIFY-04: 新 gcode 任务重置 completed 标记，可再次触发
    TC-NOTIFY-05: completed 标记独立于 layer3，不互相干扰
    TC-NOTIFY-06: 队列满时优雅丢弃，不抛异常

  防漏发（启动扫描）:
    TC-NOTIFY-07: 启动时已在第3层之后 → 初始化 layer3_notified=True
    TC-NOTIFY-08: 启动时小于第3层 → 初始化 layer3_notified=False
    TC-NOTIFY-09: 启动扫描不发送任何通知
    TC-NOTIFY-10: 启动时无通知器仍正常初始化状态
    TC-NOTIFY-11: 启动时 IDLE 状态跳过

  边界条件:
    TC-NOTIFY-12: 刚好第3层（layer=3）触发 layer3 通知
    TC-NOTIFY-13: 刚好100%触发 completed 通知
    TC-NOTIFY-14: 多台打印机独立追踪，互不干扰
    TC-NOTIFY-15: 同一任务从 layer=3 继续到 progress=100，两个事件都触发
    TC-NOTIFY-16: 状态非 RUNNING 时不触发进度事件
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from bumbo_stats.models import PrinterState, PrinterStatus
from bumbo_stats.printer.monitor import PrintMonitor
from bumbo_stats.printer.wecom import (
    WeComNotifier, WeComConfig, _PrinterNotifState,
    EVENT_LAYER3, EVENT_COMPLETED,
)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _make_status(serial, state=PrinterState.RUNNING, gcode_file="test.gcode",
                current_layer=0, total_layers=100, progress=0):
    return PrinterStatus(
        serial=serial, name="Test", state=state,
        gcode_file=gcode_file, last_updated=datetime.now(),
        current_layer=current_layer, total_layers=total_layers,
        progress=progress,
    )


def _make_wecom_config():
    return WeComConfig(
        bot_id="fake_bot_id",
        secret="fake_secret",
        chatid="fake_chatid",
        layer3=True,
        completed=True,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_wecom_notifier():
    """Mock 企业微信通知器"""
    mock = MagicMock()
    mock.notify = MagicMock()
    return mock


@pytest.fixture
def mock_manager():
    return MagicMock()


@pytest.fixture
async def mock_db():
    db = AsyncMock()
    db.snapshot_yesterday = AsyncMock()
    return db


@pytest.fixture
def broadcast_calls():
    calls = []

    async def mock_broadcast(msg):
        calls.append(msg)

    mock = MagicMock()
    mock.side_effect = mock_broadcast
    return mock, calls


@pytest.fixture
def monitor_with_wecom(mock_manager, mock_db, broadcast_calls, mock_wecom_notifier):
    """带企业微信通知器的 PrintMonitor"""
    _, calls = broadcast_calls
    mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], mock_wecom_notifier)
    mon._broadcast_calls = calls
    return mon


# ---------------------------------------------------------------------------
# TC-NOTIFY-01~05: 防重发测试（monitor 层）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestNotificationDeduplicationMonitor:
    async def test_tc_notify_01_layer3_once(self, monitor_with_wecom, mock_wecom_notifier):
        """TC-NOTIFY-01: 同一事件（layer3）多次触发只通知一次"""
        mon = monitor_with_wecom

        # 初始化状态
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"

        # 第1次：layer 从 0 -> 3，触发
        s1 = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                          current_layer=3, total_layers=100, progress=3)
        mon._check_progress_events("S001", s1)
        assert mock_wecom_notifier.notify.call_count == 1

        # 第2次：layer 继续增加到 5，不应再触发
        s2 = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                          current_layer=5, total_layers=100, progress=5)
        mon._check_progress_events("S001", s2)
        assert mock_wecom_notifier.notify.call_count == 1

        # 第3次：layer=10，也不应触发
        s3 = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                          current_layer=10, total_layers=100, progress=10)
        mon._check_progress_events("S001", s3)
        assert mock_wecom_notifier.notify.call_count == 1

    async def test_tc_notify_02_completed_once(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-02: completed 事件同任务多次达到 100% 只通知一次

        使用真实 WeComNotifier 测试其 completed_sent 标记的防重发。
        注意：layer=100 不触发 layer3（只有==3才触发），所以只有 completed 入队。
        """
        _, calls = broadcast_calls
        from bumbo_stats.config import PrinterConfig
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        wecom = WeComNotifier(_make_wecom_config(), [printer_cfg])
        wecom._queue = asyncio.Queue(maxsize=10)
        wecom._chatid = "test_chatid"
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom)
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"

        # 第1次 progress=100：只有 completed 入队（layer=100 不满足 layer==3）
        s1 = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                          current_layer=100, total_layers=100, progress=100)
        mon._check_progress_events("S001", s1)
        assert wecom._queue.qsize() == 1  # 只有 completed

        # 同一任务再次 progress=100：wecom 层 completed_sent 已设，跳过
        s2 = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                          current_layer=100, total_layers=100, progress=100)
        mon._check_progress_events("S001", s2)
        assert wecom._queue.qsize() == 1  # 未增加

    async def test_tc_notify_03_gcode_change_resets_layer3_and_notifies(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-03: 新 gcode 任务到达第3层时触发 layer3 通知

        _check_progress_events 检测到 gcode 变化时重置 layer3 标记，
        然后因 layer>=3 再次触发通知。使用真实 WeComNotifier。
        """
        _, calls = broadcast_calls
        from bumbo_stats.config import PrinterConfig
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        wecom = WeComNotifier(_make_wecom_config(), [printer_cfg])
        wecom._queue = asyncio.Queue(maxsize=10)
        wecom._chatid = "test_chatid"
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom)
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job1.gcode"

        # job1: layer=3，触发
        s1 = _make_status("S001", PrinterState.RUNNING, "job1.gcode",
                          current_layer=3, total_layers=100, progress=3)
        mon._check_progress_events("S001", s1)
        assert wecom._queue.qsize() == 1

        # job1 继续打印，不触发
        s2 = _make_status("S001", PrinterState.RUNNING, "job1.gcode",
                          current_layer=50, total_layers=100, progress=50)
        mon._check_progress_events("S001", s2)
        assert wecom._queue.qsize() == 1

        # job2: gcode 变化，_check_progress_events 重置标记并触发
        s3 = _make_status("S001", PrinterState.RUNNING, "job2.gcode",
                          current_layer=3, total_layers=80, progress=4)
        mon._check_progress_events("S001", s3)
        # wecom.notify() 检测到新 gcode，重置 layer3_sent，再触发
        assert wecom._queue.qsize() == 2

    async def test_tc_notify_04_completed_new_job_resets_via_wecom_layer(self, monitor_with_wecom, mock_wecom_notifier):
        """TC-NOTIFY-04: completed 防重发依赖 wecom 层的 reset_if_new_job

        wecom.notify() 中通过 state.reset_if_new_job(gcode) 检测新任务并重置 completed_sent。
        monitor 层本身不在 _check_progress_events 中处理 gcode 变化时的 completed 重置。
        """
        mon = monitor_with_wecom
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job1.gcode"

        # job1 完成：触发 completed（同时触发 layer3）
        s1 = _make_status("S001", PrinterState.RUNNING, "job1.gcode",
                          current_layer=100, total_layers=100, progress=100)
        mon._check_progress_events("S001", s1)
        completed_count_1 = sum(
            1 for c in mock_wecom_notifier.notify.call_args_list
            if c[0][1] == EVENT_COMPLETED
        )
        assert completed_count_1 == 1

        # job2 开始：模拟 _check_progress_events 已更新 _last_gcode
        # wecom.notify() 会检测到新 gcode 并重置 completed_sent
        mon._last_gcode["S001"] = "job2.gcode"
        s2 = _make_status("S001", PrinterState.RUNNING, "job2.gcode",
                          current_layer=100, total_layers=80, progress=100)
        mon._check_progress_events("S001", s2)
        # wecom.notify() 中检测到新 gcode，completed_sent 重置，
        # completed 再次触发
        completed_count_2 = sum(
            1 for c in mock_wecom_notifier.notify.call_args_list
            if c[0][1] == EVENT_COMPLETED
        )
        assert completed_count_2 == 2  # job2 的 completed 也触发了

    async def test_tc_notify_05_layer3_and_completed_independent(self, monitor_with_wecom, mock_wecom_notifier):
        """TC-NOTIFY-05: completed 标记独立于 layer3，不互相干扰"""
        mon = monitor_with_wecom

        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"

        # layer3 触发（layer=3，progress=3）
        s1 = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                          current_layer=3, total_layers=100, progress=3)
        mon._check_progress_events("S001", s1)
        assert mock_wecom_notifier.notify.call_count == 1
        # call_args_list[0] 第二个参数是事件类型
        assert mock_wecom_notifier.notify.call_args_list[0][0][1] == EVENT_LAYER3

        # 打印到 100%，completed 也应触发（不因 layer3 已触发而跳过）
        s2 = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                          current_layer=100, total_layers=100, progress=100)
        mon._check_progress_events("S001", s2)
        assert mock_wecom_notifier.notify.call_count == 2
        # 第二次是 completed 事件
        assert mock_wecom_notifier.notify.call_args_list[1][0][1] == EVENT_COMPLETED

    async def test_tc_notify_06_no_notify_without_wecom(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-06: 无企业微信通知器时不崩溃"""
        _, calls = broadcast_calls
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom_notifier=None)

        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"

        # 不应抛异常
        s = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                         current_layer=3, total_layers=100, progress=3)
        mon._check_progress_events("S001", s)
        # 无 wecom_notifier 时不触发任何错误

    async def test_tc_notify_16_non_running_skips_progress(self, monitor_with_wecom, mock_wecom_notifier):
        """TC-NOTIFY-16: 状态非 RUNNING 时不触发进度事件"""
        mon = monitor_with_wecom

        # IDLE 状态即使 layer=50 也不触发
        mon._last_states["S001"] = PrinterState.IDLE
        s = _make_status("S001", PrinterState.IDLE, "job.gcode",
                         current_layer=50, total_layers=100, progress=50)
        mon._check_progress_events("S001", s)
        mock_wecom_notifier.notify.assert_not_called()


# ---------------------------------------------------------------------------
# TC-NOTIFY-07~11: 防漏发测试（启动扫描）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestStartupScan:
    async def test_tc_notify_07_startup_above_layer3(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-07: 启动时已在第3层之后 → 初始化 layer3_notified=True（不发送通知）"""
        _, calls = broadcast_calls
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom_notifier=None)

        mock_manager.get_all_statuses.return_value = {
            "S001": _make_status("S001", PrinterState.RUNNING, "old_job.gcode",
                                 current_layer=50, total_layers=100, progress=50),
        }

        await mon._scan_startup_notifications()

        # layer3_notified 初始化为 True（已超第3层）
        assert mon._layer3_notified["S001"] is True
        # _last_gcode 也正确初始化
        assert mon._last_gcode["S001"] == "old_job.gcode"

    async def test_tc_notify_08_startup_below_layer3(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-08: 启动时小于第3层 → 初始化 layer3_notified=False（不发送通知）"""
        _, calls = broadcast_calls
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom_notifier=None)

        mock_manager.get_all_statuses.return_value = {
            "S001": _make_status("S001", PrinterState.RUNNING, "new_job.gcode",
                                 current_layer=1, total_layers=100, progress=1),
        }

        await mon._scan_startup_notifications()

        # layer3_notified 初始化为 False（未达第3层）
        assert mon._layer3_notified["S001"] is False
        assert mon._last_gcode["S001"] == "new_job.gcode"

    async def test_tc_notify_09_startup_does_not_send_notification(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-09: 启动扫描不发送任何通知（历史事件不补发）"""
        _, calls = broadcast_calls
        wecom_mock = MagicMock()
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom_mock)

        # 已超过第3层且已完成
        mock_manager.get_all_statuses.return_value = {
            "S001": _make_status("S001", PrinterState.RUNNING, "old_job.gcode",
                                 current_layer=100, total_layers=100, progress=100),
        }

        await mon._scan_startup_notifications()

        # 状态初始化正确
        assert mon._layer3_notified["S001"] is True
        # 不发送任何通知
        wecom_mock.notify.assert_not_called()

    async def test_tc_notify_10_startup_no_wecom_init_still_works(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-10: 启动时无通知器仍正常初始化状态"""
        _, calls = broadcast_calls
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom_notifier=None)

        mock_manager.get_all_statuses.return_value = {
            "S001": _make_status("S001", PrinterState.RUNNING, "job.gcode",
                                 current_layer=50, total_layers=100, progress=50),
        }

        # 不抛异常
        await mon._scan_startup_notifications()
        assert mon._layer3_notified["S001"] is True
        assert mon._last_gcode["S001"] == "job.gcode"

    async def test_tc_notify_11_startup_skips_idle(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-11: 启动时 IDLE 状态跳过初始化"""
        _, calls = broadcast_calls
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom_notifier=None)

        mock_manager.get_all_statuses.return_value = {
            "S001": _make_status("S001", PrinterState.IDLE, "", current_layer=0, total_layers=0, progress=0),
        }

        await mon._scan_startup_notifications()

        # IDLE 状态不初始化（保持默认值）
        assert "S001" not in mon._layer3_notified

    async def test_tc_notify_26_startup_syncs_wecom_state_above_layer3(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-26: 启动时已在第3层之后，同步 WeComNotifier 状态（last_gcode 和 layer3_sent）"""
        _, calls = broadcast_calls
        from bumbo_stats.config import PrinterConfig
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        wecom = WeComNotifier(_make_wecom_config(), [printer_cfg])
        wecom._queue = asyncio.Queue(maxsize=10)
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom)

        # 验证初始化时 WeComNotifier 状态
        assert wecom._states["S001"].last_gcode == ""
        assert wecom._states["S001"].layer3_sent is False

        mock_manager.get_all_statuses.return_value = {
            "S001": _make_status("S001", PrinterState.RUNNING, "old_job.gcode",
                                 current_layer=50, total_layers=100, progress=50),
        }

        await mon._scan_startup_notifications()

        # 同步验证：last_gcode 和 layer3_sent 都被正确初始化
        assert mon._layer3_notified["S001"] is True
        assert wecom._states["S001"].last_gcode == "old_job.gcode"
        assert wecom._states["S001"].layer3_sent is True
        # 不发送通知
        assert wecom._queue.qsize() == 0

    async def test_tc_notify_27_startup_syncs_wecom_state_below_layer3(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-27: 启动时小于第3层，同步 WeComNotifier 的 last_gcode（不设置 layer3_sent）"""
        _, calls = broadcast_calls
        from bumbo_stats.config import PrinterConfig
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        wecom = WeComNotifier(_make_wecom_config(), [printer_cfg])
        wecom._queue = asyncio.Queue(maxsize=10)
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom)

        mock_manager.get_all_statuses.return_value = {
            "S001": _make_status("S001", PrinterState.RUNNING, "new_job.gcode",
                                 current_layer=1, total_layers=100, progress=1),
        }

        await mon._scan_startup_notifications()

        # 同步验证：last_gcode 被初始化，layer3_sent 保持 False
        assert mon._layer3_notified["S001"] is False
        assert wecom._states["S001"].last_gcode == "new_job.gcode"
        assert wecom._states["S001"].layer3_sent is False
        # 不发送通知
        assert wecom._queue.qsize() == 0


# ---------------------------------------------------------------------------
# TC-NOTIFY-12~15: 边界条件测试
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestNotificationBoundaryConditions:
    async def test_tc_notify_12_exactly_layer3(self, monitor_with_wecom, mock_wecom_notifier):
        """TC-NOTIFY-12: 刚好第3层（layer=3）触发 layer3 通知"""
        mon = monitor_with_wecom
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"

        s = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                         current_layer=3, total_layers=100, progress=3)
        mon._check_progress_events("S001", s)

        mock_wecom_notifier.notify.assert_called_once()
        args = mock_wecom_notifier.notify.call_args[0]
        assert args[1] == EVENT_LAYER3

    async def test_tc_notify_13_exactly_100_percent(self, monitor_with_wecom, mock_wecom_notifier):
        """TC-NOTIFY-13: 刚好100%只触发 completed 通知（layer=100 不触发 layer3）"""
        mon = monitor_with_wecom
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"

        s = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                         current_layer=100, total_layers=100, progress=100)
        mon._check_progress_events("S001", s)

        # 只有 completed 事件触发（layer=100 不满足 layer==3）
        completed_calls = [c for c in mock_wecom_notifier.notify.call_args_list
                         if c[0][1] == EVENT_COMPLETED]
        assert len(completed_calls) == 1
        assert mock_wecom_notifier.notify.call_count == 1

    async def test_tc_notify_14_multiple_printers_independent(self, monitor_with_wecom,
                                                              mock_wecom_notifier):
        """TC-NOTIFY-14: 多台打印机独立追踪，互不干扰"""
        mon = monitor_with_wecom

        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job1.gcode"
        mon._last_states["S002"] = PrinterState.RUNNING
        mon._last_gcode["S002"] = "job2.gcode"

        # S001 到达第3层
        s1 = _make_status("S001", PrinterState.RUNNING, "job1.gcode",
                          current_layer=3, total_layers=100, progress=3)
        mon._check_progress_events("S001", s1)

        # S002 到达第3层
        s2 = _make_status("S002", PrinterState.RUNNING, "job2.gcode",
                          current_layer=3, total_layers=100, progress=3)
        mon._check_progress_events("S002", s2)

        # 两台都触发了
        assert mock_wecom_notifier.notify.call_count == 2

        # S001 继续打印到第10层，不再触发
        s3 = _make_status("S001", PrinterState.RUNNING, "job1.gcode",
                          current_layer=10, total_layers=100, progress=10)
        mon._check_progress_events("S001", s3)
        assert mock_wecom_notifier.notify.call_count == 2

        # S002 继续打印到第10层，不再触发
        s4 = _make_status("S002", PrinterState.RUNNING, "job2.gcode",
                          current_layer=10, total_layers=100, progress=10)
        mon._check_progress_events("S002", s4)
        assert mock_wecom_notifier.notify.call_count == 2

    async def test_tc_notify_28_above_layer3_no_trigger(self, monitor_with_wecom, mock_wecom_notifier):
        """TC-NOTIFY-28: 层数大于3时不触发 layer3 通知（只有==3才触发）"""
        mon = monitor_with_wecom
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"

        # 从第4层开始（不是第3层），不应触发 layer3
        s = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                         current_layer=4, total_layers=100, progress=4)
        mon._check_progress_events("S001", s)
        mock_wecom_notifier.notify.assert_not_called()

    async def test_tc_notify_15_layer3_then_completed_same_job(self, monitor_with_wecom,
                                                                 mock_wecom_notifier):
        """TC-NOTIFY-15: 同一任务从 layer=3 继续到 progress=100，两个事件按序触发"""
        mon = monitor_with_wecom

        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"

        # layer=3 → layer3 事件
        s1 = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                          current_layer=3, total_layers=100, progress=3)
        mon._check_progress_events("S001", s1)
        assert mock_wecom_notifier.notify.call_count == 1
        assert mock_wecom_notifier.notify.call_args_list[0][0][1] == EVENT_LAYER3

        # 继续打印到 100% → completed 事件
        s2 = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                          current_layer=100, total_layers=100, progress=100)
        mon._check_progress_events("S001", s2)
        assert mock_wecom_notifier.notify.call_count == 2
        assert mock_wecom_notifier.notify.call_args_list[1][0][1] == EVENT_COMPLETED


# ---------------------------------------------------------------------------
# TC-NOTIFY-17~: WeComNotifier 层去重测试
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestWeComNotifierDeduplication:
    async def test_tc_notify_17_wecom_deduplicates_layer3(self):
        """TC-NOTIFY-17: WeComNotifier 层对 layer3 事件去重"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        notifier = WeComNotifier(_make_wecom_config(), [printer_cfg])
        notifier._queue = asyncio.Queue(maxsize=10)
        notifier._chatid = "test_chatid"  # 绕过 chatid 等待

        # 模拟状态（第1次）
        status = _make_status("S001", current_layer=3, total_layers=100, progress=3)

        # 第1次通知：入队
        notifier.notify("S001", EVENT_LAYER3, status)
        assert notifier._queue.qsize() == 1

        # 第2次通知（同事件同任务）：应被 wecom 层去重，不入队
        notifier.notify("S001", EVENT_LAYER3, status)
        assert notifier._queue.qsize() == 1

    async def test_tc_notify_18_wecom_deduplicates_completed(self):
        """TC-NOTIFY-18: WeComNotifier 层对 completed 事件去重"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        notifier = WeComNotifier(_make_wecom_config(), [printer_cfg])
        notifier._queue = asyncio.Queue(maxsize=10)
        notifier._chatid = "test_chatid"

        status = _make_status("S001", current_layer=100, total_layers=100, progress=100)

        notifier.notify("S001", EVENT_COMPLETED, status)
        assert notifier._queue.qsize() == 1

        notifier.notify("S001", EVENT_COMPLETED, status)
        assert notifier._queue.qsize() == 1

    async def test_tc_notify_19_wecom_new_job_resets_layer3(self):
        """TC-NOTIFY-19: WeComNotifier 检测到新任务时重置 layer3_sent"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        notifier = WeComNotifier(_make_wecom_config(), [printer_cfg])
        notifier._queue = asyncio.Queue(maxsize=10)
        notifier._chatid = "test_chatid"

        # job1 触发 layer3
        status1 = _make_status("S001", gcode_file="job1.gcode",
                                current_layer=3, total_layers=100, progress=3)
        notifier.notify("S001", EVENT_LAYER3, status1)
        assert notifier._queue.qsize() == 1

        # 再次触发 job1 → 去重
        notifier.notify("S001", EVENT_LAYER3, status1)
        assert notifier._queue.qsize() == 1

        # job2 触发 → 重置后可再发
        status2 = _make_status("S001", gcode_file="job2.gcode",
                               current_layer=3, total_layers=100, progress=3)
        notifier.notify("S001", EVENT_LAYER3, status2)
        assert notifier._queue.qsize() == 2

    async def test_tc_notify_20_wecom_queue_full_drops_gracefully(self):
        """TC-NOTIFY-20: 队列满时优雅丢弃，不抛异常"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        notifier = WeComNotifier(_make_wecom_config(), [printer_cfg])
        # 队列大小为 1
        notifier._queue = asyncio.Queue(maxsize=1)
        notifier._chatid = "test_chatid"

        status = _make_status("S001", current_layer=3, total_layers=100, progress=3)

        # 第1次入队成功
        notifier.notify("S001", EVENT_LAYER3, status)
        assert notifier._queue.qsize() == 1

        # 第2次：队列已满，不抛异常，静默丢弃
        notifier.notify("S001", EVENT_LAYER3, status)
        assert notifier._queue.qsize() == 1  # 仍是1

    async def test_tc_notify_21_wecom_disabled_event_no_queue(self):
        """TC-NOTIFY-21: 禁用的通知事件不入队"""
        from bumbo_stats.config import PrinterConfig

        cfg = _make_wecom_config()
        cfg.layer3 = False  # 禁用 layer3
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        notifier = WeComNotifier(cfg, [printer_cfg])
        notifier._queue = asyncio.Queue(maxsize=10)
        notifier._chatid = "test_chatid"

        status = _make_status("S001", current_layer=3, total_layers=100, progress=3)
        notifier.notify("S001", EVENT_LAYER3, status)
        assert notifier._queue.qsize() == 0  # 未入队

    async def test_tc_notify_22_wecom_unknown_printer_ignored(self):
        """TC-NOTIFY-22: 未知的 serial 不入队，不崩溃"""
        from bumbo_stats.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        notifier = WeComNotifier(_make_wecom_config(), [printer_cfg])
        notifier._queue = asyncio.Queue(maxsize=10)

        # 未知打印机
        notifier.notify("UNKNOWN_SERIAL", EVENT_LAYER3, None)
        # 不抛异常

    async def test_tc_notify_23_printer_notif_state_reset_if_new_job(self):
        """TC-NOTIFY-23: _PrinterNotifState.reset_if_new_job 正确重置"""
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

        # 空 gcode 不重置
        state.layer3_sent = True
        state.completed_sent = True
        state.reset_if_new_job("")
        assert state.layer3_sent is True
        assert state.completed_sent is True


# ---------------------------------------------------------------------------
# TC-NOTIFY-24~: 双层去重协调测试
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDoubleLayerDeduplication:
    async def test_tc_notify_24_full_flow_gcode_change_via_handle_status_update(
        self, mock_manager, mock_db, broadcast_calls
    ):
        """TC-NOTIFY-24: handle_status_update 完整流程正确处理 gcode 变化

        验证实际调用路径 handle_status_update 的完整行为：
        _check_progress_events 先执行 → _handle_state_change → broadcast

        当 gcode 变化但状态不变（RUNNING->RUNNING）时：
        _check_progress_events 检测 gcode 变化，重置 layer3 标记并触发。
        这是正确的行为，因为 _check_progress_events 先于 _handle_state_change 执行。
        """
        _, calls = broadcast_calls
        from bumbo_stats.config import PrinterConfig
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        wecom = WeComNotifier(_make_wecom_config(), [printer_cfg])
        wecom._queue = asyncio.Queue(maxsize=10)
        wecom._chatid = "test_chatid"
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom)

        # job1: layer=3，触发
        await mon.handle_status_update("S001", _make_status(
            "S001", PrinterState.RUNNING, "job1.gcode",
            current_layer=3, total_layers=100, progress=3
        ))
        assert mon._layer3_notified["S001"] is True
        assert wecom._queue.qsize() == 1

        # job1 继续：不再触发
        await mon.handle_status_update("S001", _make_status(
            "S001", PrinterState.RUNNING, "job1.gcode",
            current_layer=50, total_layers=100, progress=50
        ))
        assert wecom._queue.qsize() == 1

        # job2: gcode 变化，_check_progress_events 先检测并重置标记，然后 layer=3 触发
        await mon.handle_status_update("S001", _make_status(
            "S001", PrinterState.RUNNING, "job2.gcode",
            current_layer=3, total_layers=80, progress=4
        ))
        # gcode 变化时：layer3 重置（为 False），然后再次触发 layer3（又设为 True）
        # 验证：layer3 事件再次入队（queue 从 1 增到 2）
        assert wecom._queue.qsize() == 2

        # job2 完成：completed 也触发
        await mon.handle_status_update("S001", _make_status(
            "S001", PrinterState.RUNNING, "job2.gcode",
            current_layer=80, total_layers=80, progress=100
        ))
        # completed 事件入队（queue 从 2 增到 3）
        assert wecom._queue.qsize() == 3

    async def test_tc_notify_25_both_layers_prevent_duplicate(self, mock_manager, mock_db,
                                                               broadcast_calls):
        """TC-NOTIFY-25: monitor 和 wecom 双层去重，共同防止重复"""
        _, calls = broadcast_calls

        from bumbo_stats.config import PrinterConfig
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        wecom = WeComNotifier(_make_wecom_config(), [printer_cfg])
        wecom._queue = asyncio.Queue(maxsize=10)
        wecom._chatid = "test_chatid"

        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom)

        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"

        # 第1次到达第3层
        s1 = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                          current_layer=3, total_layers=100, progress=3)
        mon._check_progress_events("S001", s1)
        assert wecom._queue.qsize() == 1

        # 同任务继续打印（layer=5）：monitor 层已标记，wecom 层也标记，不会再发
        s2 = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                          current_layer=5, total_layers=100, progress=5)
        mon._check_progress_events("S001", s2)
        assert wecom._queue.qsize() == 1  # 未增加

        # 同任务打印到 100%：completed 未发，应触发
        s3 = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                          current_layer=100, total_layers=100, progress=100)
        mon._check_progress_events("S001", s3)
        assert wecom._queue.qsize() == 2  # completed 入队

        # 再次同一任务到 100%：不重复
        s4 = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                          current_layer=100, total_layers=100, progress=100)
        mon._check_progress_events("S001", s4)
        assert wecom._queue.qsize() == 2  # 仍是2
