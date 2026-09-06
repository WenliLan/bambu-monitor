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

from bumbo_stats.models import PrinterState, PrinterStatus, HMSError
from bumbo_stats.printer.monitor import PrintMonitor
from bumbo_stats.printer.wecom import (
    WeComNotifier, WeComConfig, _PrinterNotifState,
    EVENT_LAYER3, EVENT_COMPLETED, EVENT_ERROR, EVENT_PROGRESS50,
    normalize_gcode,
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


def _make_wecom_config():
    return WeComConfig(
        bot_id="fake_bot_id",
        secret="fake_secret",
        chatid="fake_chatid",
        layer3=True,
        completed=True,
        error=True,
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

        # job1 继续打印：layer3 不再触发，但 progress=50 触发 progress50
        s2 = _make_status("S001", PrinterState.RUNNING, "job1.gcode",
                          current_layer=50, total_layers=100, progress=50)
        mon._check_progress_events("S001", s2)
        assert wecom._queue.qsize() == 2

        # job2: gcode 变化，_check_progress_events 重置标记并触发 layer3
        s3 = _make_status("S001", PrinterState.RUNNING, "job2.gcode",
                          current_layer=3, total_layers=80, progress=4)
        mon._check_progress_events("S001", s3)
        # wecom.notify() 检测到新 gcode，重置 layer3_sent，再触发
        assert wecom._queue.qsize() == 3

    async def test_tc_notify_04_completed_new_job_resets_via_wecom_layer(self, monitor_with_wecom, mock_wecom_notifier):
        """TC-NOTIFY-04: completed 防重发依赖 wecom 层的 reset_if_new_job

        wecom.notify() 中通过 state.reset_if_new_job(gcode) 检测新任务并重置 completed_sent。
        monitor 层本身不在 _check_progress_events 中处理 gcode 变化时的 completed 重置。
        """
        mon = monitor_with_wecom
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job1.gcode"

        # job1 完成：触发 completed（progress=100 时 layer3/progress50 均不触发）
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
        """TC-NOTIFY-08: 启动时小于第3层 → 也标记已发（运行中任务不补发，启动数据可能不完整）"""
        _, calls = broadcast_calls
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom_notifier=None)

        mock_manager.get_all_statuses.return_value = {
            "S001": _make_status("S001", PrinterState.RUNNING, "new_job.gcode",
                                 current_layer=1, total_layers=100, progress=1),
        }

        await mon._scan_startup_notifications()

        # 运行中任务一律标记已发（启动时 MQTT 数据可能不完整，防止误发）
        assert mon._layer3_notified["S001"] is True
        assert mon._progress50_notified["S001"] is True
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
        """TC-NOTIFY-27: 启动时小于第3层，同样同步标记已发（运行中任务不补发）"""
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

        # 同步验证：last_gcode 被初始化，运行中任务 layer3_sent 也标记 True
        assert mon._layer3_notified["S001"] is True
        assert wecom._states["S001"].last_gcode == "new_job.gcode"
        assert wecom._states["S001"].layer3_sent is True
        assert wecom._states["S001"].progress50_sent is True
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

    async def test_tc_notify_46_progress99_triggers_completed(self, monitor_with_wecom, mock_wecom_notifier):
        """TC-NOTIFY-46: 进度 99% 即触发完成通知（截图能拍到成品），不触发 layer3/progress50"""
        mon = monitor_with_wecom
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"

        s = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                         current_layer=99, total_layers=100, progress=99)
        mon._check_progress_events("S001", s)

        events = [c[0][1] for c in mock_wecom_notifier.notify.call_args_list]
        assert events == [EVENT_COMPLETED]

    async def test_tc_notify_47_progress98_not_completed(self, monitor_with_wecom, mock_wecom_notifier):
        """TC-NOTIFY-47: 进度 98% 不触发完成通知（未达 99% 阈值，仅 layer3/progress50）"""
        mon = monitor_with_wecom
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"

        s = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                         current_layer=98, total_layers=100, progress=98)
        mon._check_progress_events("S001", s)

        events = [c[0][1] for c in mock_wecom_notifier.notify.call_args_list]
        assert EVENT_COMPLETED not in events
        assert set(events) == {EVENT_LAYER3, EVENT_PROGRESS50}

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

    async def test_tc_notify_28_above_layer3_triggers(self, monitor_with_wecom, mock_wecom_notifier):
        """TC-NOTIFY-28: 层数大于3时也触发 layer3 通知（MQTT 跳层上报不漏发），且仅触发一次"""
        mon = monitor_with_wecom
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"

        # MQTT 跳层：从未上报过第3层，直接报第4层 → 仍应触发 layer3
        s = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                         current_layer=4, total_layers=100, progress=4)
        mon._check_progress_events("S001", s)
        assert mock_wecom_notifier.notify.call_count == 1
        assert mock_wecom_notifier.notify.call_args[0][1] == EVENT_LAYER3

        # 后续层数继续上涨不再重复触发
        s2 = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                          current_layer=10, total_layers=100, progress=10)
        mon._check_progress_events("S001", s2)
        assert mock_wecom_notifier.notify.call_count == 1

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

    async def test_tc_notify_51_wecom_master_switch_disabled_no_queue(self):
        """TC-NOTIFY-51: 总开关 enabled=False 时，所有事件均不入队（不限于单项事件）"""
        from bumbo_stats.config import PrinterConfig

        cfg = _make_wecom_config()
        cfg.enabled = False  # 禁用总开关
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        notifier = WeComNotifier(cfg, [printer_cfg])
        notifier._queue = asyncio.Queue(maxsize=10)
        notifier._chatid = "test_chatid"

        # 任意事件都应被总开关拦截
        status = _make_status("S001", current_layer=3, total_layers=100, progress=3)
        notifier.notify("S001", EVENT_LAYER3, status)
        notifier.notify("S001", EVENT_PROGRESS50, status)
        notifier.notify("S001", EVENT_COMPLETED, status)

        hms = [HMSError(code=0x0B01, severity="error", message="堵嘴检测")]
        notifier.notify("S001", EVENT_ERROR, status, errors=hms)

        assert notifier._queue.qsize() == 0

    async def test_tc_notify_52_wecom_master_switch_disabled_start_skipped(self):
        """TC-NOTIFY-52: enabled=False 时 start() 不创建后台任务、也不重连 WebSocket"""
        from bumbo_stats.config import PrinterConfig

        cfg = _make_wecom_config()
        cfg.enabled = False
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        notifier = WeComNotifier(cfg, [printer_cfg])

        await notifier.start()
        # 启动被跳过，_running 仍为 False，后台任务未创建
        assert notifier._running is False
        assert notifier._task is None

    async def test_tc_notify_53_wecom_update_config_returns_changed(self):
        """TC-NOTIFY-53: update_config 返回 enabled 状态是否变化（用于触发启停切换）"""
        from bumbo_stats.config import PrinterConfig

        cfg = _make_wecom_config()
        cfg.enabled = True
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        notifier = WeComNotifier(cfg, [printer_cfg])

        # enabled 未变（其他字段变更） → 返回 False
        new_cfg = WeComConfig(
            enabled=True, bot_id="fake_bot_id", secret="fake_secret",
            chatid="", layer3=False, progress50=True, completed=True, error=True,
        )
        assert notifier.update_config(new_cfg) is False

        # enabled 切到 False → 返回 True
        new_cfg2 = WeComConfig(
            enabled=False, bot_id="fake_bot_id", secret="fake_secret",
            chatid="", layer3=True, progress50=True, completed=True, error=True,
        )
        assert notifier.update_config(new_cfg2) is True
        assert notifier._cfg.enabled is False

        # enabled 切回 True → 返回 True
        new_cfg3 = WeComConfig(
            enabled=True, bot_id="fake_bot_id", secret="fake_secret",
            chatid="", layer3=True, progress50=True, completed=True, error=True,
        )
        assert notifier.update_config(new_cfg3) is True
        assert notifier._cfg.enabled is True

        # 再次未变 → 返回 False
        assert notifier.update_config(new_cfg3) is False

    async def test_tc_notify_54_wecom_master_switch_toggle_allow_queue(self):
        """TC-NOTIFY-54: 关闭 → 开启后，事件可重新入队（验证状态可逆）"""
        from bumbo_stats.config import PrinterConfig

        cfg = _make_wecom_config()
        cfg.enabled = False
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        notifier = WeComNotifier(cfg, [printer_cfg])
        notifier._queue = asyncio.Queue(maxsize=10)
        notifier._chatid = "test_chatid"

        status = _make_status("S001", current_layer=3, total_layers=100, progress=3)

        # 关闭时入队被拦截
        notifier.notify("S001", EVENT_LAYER3, status)
        assert notifier._queue.qsize() == 0

        # 开启后（update_config 仅改 _cfg，后台任务启停由 reload 协调）
        new_cfg = WeComConfig(
            enabled=True, bot_id="fake_bot_id", secret="fake_secret",
            chatid="test_chatid", layer3=True, progress50=True,
            completed=True, error=True,
        )
        notifier.update_config(new_cfg)
        notifier.notify("S001", EVENT_LAYER3, status)
        assert notifier._queue.qsize() == 1

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

        # job1 继续：layer3 不再触发，但 progress=50 触发 progress50
        await mon.handle_status_update("S001", _make_status(
            "S001", PrinterState.RUNNING, "job1.gcode",
            current_layer=50, total_layers=100, progress=50
        ))
        assert wecom._queue.qsize() == 2

        # job2: gcode 变化，_check_progress_events 先检测并重置标记，然后 layer=3 触发
        await mon.handle_status_update("S001", _make_status(
            "S001", PrinterState.RUNNING, "job2.gcode",
            current_layer=3, total_layers=80, progress=4
        ))
        # gcode 变化时：layer3 重置（为 False），然后再次触发 layer3（又设为 True）
        # 验证：layer3 事件再次入队（queue 从 2 增到 3）
        assert wecom._queue.qsize() == 3

        # job2 完成：completed 也触发
        await mon.handle_status_update("S001", _make_status(
            "S001", PrinterState.RUNNING, "job2.gcode",
            current_layer=80, total_layers=80, progress=100
        ))
        # completed 事件入队（queue 从 3 增到 4）
        assert wecom._queue.qsize() == 4

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


# ---------------------------------------------------------------------------
# TC-NOTIFY-39~43: 50% 进度通知测试
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestProgress50Notification:
    async def test_tc_notify_39_progress50_triggers_once(self, monitor_with_wecom, mock_wecom_notifier):
        """TC-NOTIFY-39: 进度达到 50% 触发 progress50 通知，且仅触发一次"""
        mon = monitor_with_wecom
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"
        mon._notify_job_gcode["S001"] = "job.gcode"  # 任务中途，避免首条消息被判为新任务
        mon._layer3_notified["S001"] = True  # 隔离 layer3

        s1 = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                          current_layer=50, total_layers=100, progress=50)
        mon._check_progress_events("S001", s1)
        assert mock_wecom_notifier.notify.call_count == 1
        assert mock_wecom_notifier.notify.call_args[0][1] == EVENT_PROGRESS50

        # 进度继续上涨不重复触发
        s2 = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                          current_layer=70, total_layers=100, progress=70)
        mon._check_progress_events("S001", s2)
        assert mock_wecom_notifier.notify.call_count == 1

    async def test_tc_notify_40_progress50_skip_jump_still_triggers(self, monitor_with_wecom, mock_wecom_notifier):
        """TC-NOTIFY-40: MQTT 跳过 50%（如 49→62）仍触发 progress50（>= 判断）"""
        mon = monitor_with_wecom
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"
        mon._notify_job_gcode["S001"] = "job.gcode"  # 任务中途，避免首条消息被判为新任务
        mon._layer3_notified["S001"] = True  # 隔离 layer3

        s = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                         current_layer=62, total_layers=100, progress=62)
        mon._check_progress_events("S001", s)
        assert mock_wecom_notifier.notify.call_count == 1
        assert mock_wecom_notifier.notify.call_args[0][1] == EVENT_PROGRESS50

    async def test_tc_notify_41_progress100_not_trigger_progress50(self, monitor_with_wecom, mock_wecom_notifier):
        """TC-NOTIFY-41: progress=100 不触发 progress50（只触发 completed）"""
        mon = monitor_with_wecom
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"
        mon._layer3_notified["S001"] = True  # 隔离 layer3

        s = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                         current_layer=100, total_layers=100, progress=100)
        mon._check_progress_events("S001", s)

        events = [c[0][1] for c in mock_wecom_notifier.notify.call_args_list]
        assert EVENT_PROGRESS50 not in events
        assert events == [EVENT_COMPLETED]

    async def test_tc_notify_42_progress50_resets_on_new_job(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-42: 新 gcode 任务重置 progress50 标记，可再次触发"""
        _, calls = broadcast_calls
        from bumbo_stats.config import PrinterConfig
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        cfg = _make_wecom_config()
        cfg.layer3 = False  # 隔离 layer3
        wecom = WeComNotifier(cfg, [printer_cfg])
        wecom._queue = asyncio.Queue(maxsize=10)
        wecom._chatid = "test_chatid"
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom)
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job1.gcode"

        # job1 达到 50%
        s1 = _make_status("S001", PrinterState.RUNNING, "job1.gcode",
                          current_layer=50, total_layers=100, progress=50)
        mon._check_progress_events("S001", s1)
        assert wecom._queue.qsize() == 1
        assert mon._progress50_notified["S001"] is True

        # job2：gcode 变化重置标记，再次达到 50% 可再触发
        s2 = _make_status("S001", PrinterState.RUNNING, "job2.gcode",
                          current_layer=40, total_layers=80, progress=55)
        mon._check_progress_events("S001", s2)
        assert wecom._queue.qsize() == 2

    async def test_tc_notify_43_progress50_disabled_in_config(self):
        """TC-NOTIFY-43: 配置 progress50=False 时不入队；wecom 层对 progress50 去重"""
        from bumbo_stats.config import PrinterConfig
        printer_cfg = PrinterConfig(serial="S001", name="P1")

        # 禁用 progress50：不入队
        cfg = _make_wecom_config()
        cfg.progress50 = False
        notifier = WeComNotifier(cfg, [printer_cfg])
        notifier._queue = asyncio.Queue(maxsize=10)
        notifier._chatid = "test_chatid"
        status = _make_status("S001", current_layer=50, total_layers=100, progress=50)
        notifier.notify("S001", EVENT_PROGRESS50, status)
        assert notifier._queue.qsize() == 0

        # 启用时：第1次入队，第2次去重
        notifier2 = WeComNotifier(_make_wecom_config(), [printer_cfg])
        notifier2._queue = asyncio.Queue(maxsize=10)
        notifier2._chatid = "test_chatid"
        notifier2.notify("S001", EVENT_PROGRESS50, status)
        assert notifier2._queue.qsize() == 1
        notifier2.notify("S001", EVENT_PROGRESS50, status)
        assert notifier2._queue.qsize() == 1


# ---------------------------------------------------------------------------
# TC-NOTIFY-44~45: gcode 文件名归一化（云端任务名 vs MQTT 文件名）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestGcodeNormalization:
    async def test_tc_notify_44_normalize_gcode(self):
        """TC-NOTIFY-44: normalize_gcode 去扩展名归一化"""
        assert normalize_gcode("CUBE_2.4 wide.3mf") == "CUBE_2.4 wide"
        assert normalize_gcode("CUBE_2.4 wide") == "CUBE_2.4 wide"
        assert normalize_gcode("job.gcode") == "job"
        assert normalize_gcode("job.gcode.3mf") == "job"
        assert normalize_gcode("JOB.3MF") == "JOB"
        assert normalize_gcode("") == ""
        assert normalize_gcode(None) == ""

    async def test_tc_notify_45_extension_mismatch_not_new_job(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-45: 启动补录的云端任务名与 MQTT 文件名(.3mf)视为同一任务，不重复通知

        回归场景：启动扫描用云端任务名（无扩展名）标记已发，
        首条 MQTT 消息带 .3mf 扩展名被误判为新任务而重置标记、补发通知。
        """
        _, calls = broadcast_calls
        from bumbo_stats.config import PrinterConfig
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        wecom = WeComNotifier(_make_wecom_config(), [printer_cfg])
        wecom._queue = asyncio.Queue(maxsize=10)
        wecom._chatid = "test_chatid"
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom)

        # 模拟启动扫描：云端任务名（无扩展名）+ 标记已发
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "CUBE_2.4 wide"
        mon._notify_job_gcode["S001"] = "CUBE_2.4 wide"
        mon._layer3_notified["S001"] = True
        mon._progress50_notified["S001"] = True
        wecom._states["S001"].last_gcode = "CUBE_2.4 wide"
        wecom._states["S001"].layer3_sent = True
        wecom._states["S001"].progress50_sent = True

        # 首条 MQTT 消息：同一任务但文件名带 .3mf，不应重置标记、不应触发通知
        s = _make_status("S001", PrinterState.RUNNING, "CUBE_2.4 wide.3mf",
                         current_layer=293, total_layers=485, progress=58)
        mon._check_progress_events("S001", s)
        assert wecom._queue.qsize() == 0
        assert mon._layer3_notified["S001"] is True
        assert mon._progress50_notified["S001"] is True

        # 真正的新任务仍能正常触发
        mon._last_gcode["S001"] = "CUBE_2.4 wide.3mf"
        s2 = _make_status("S001", PrinterState.RUNNING, "other_job.3mf",
                          current_layer=3, total_layers=100, progress=3)
        mon._check_progress_events("S001", s2)
        assert wecom._queue.qsize() == 1


# ---------------------------------------------------------------------------
# TC-NOTIFY-48~50: 新任务重置时机（FINISH 窗口切任务 / 同名重打 / 断线重连）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestNewJobResetTiming:
    async def test_tc_notify_48_gcode_flip_in_finish_window(self, monitor_with_wecom, mock_wecom_notifier):
        """TC-NOTIFY-48: 任务切换发生在 FINISH 窗口，回到 RUNNING 后仍能重置并触发 layer3

        回归场景：gcode 翻转时状态非 RUNNING，逐条消息比较看不到变化，
        旧逻辑下标记永不重置导致新任务漏发通知。
        """
        mon = monitor_with_wecom
        # job1 已完成通知周期
        mon._last_states["S001"] = PrinterState.FINISH
        mon._last_gcode["S001"] = "job1.3mf"
        mon._notify_job_gcode["S001"] = "job1.3mf"
        mon._layer3_notified["S001"] = True
        mon._progress50_notified["S001"] = True

        # FINISH 窗口内 gcode 已翻转为 job2，不处理进度事件
        s_finish = _make_status("S001", PrinterState.FINISH, "job2.3mf",
                                current_layer=0, total_layers=0, progress=0)
        mon._check_progress_events("S001", s_finish)
        assert mock_wecom_notifier.notify.call_count == 0

        # 回到 RUNNING：与当前通知周期任务（job1）比较检测到新任务，重置并触发
        s_run = _make_status("S001", PrinterState.RUNNING, "job2.3mf",
                             current_layer=3, total_layers=100, progress=2)
        mon._check_progress_events("S001", s_run)
        events = [c[0][1] for c in mock_wecom_notifier.notify.call_args_list]
        assert events == [EVENT_LAYER3]

    async def test_tc_notify_49_reprint_same_file_resets(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-49: 打完后重打同名文件，FINISH→RUNNING 重置两层标记可再次通知"""
        _, calls = broadcast_calls
        from bumbo_stats.config import PrinterConfig
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        wecom = WeComNotifier(_make_wecom_config(), [printer_cfg])
        wecom._queue = asyncio.Queue(maxsize=10)
        wecom._chatid = "test_chatid"
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom)

        # job 已打完一单：两层标记均为已发
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_definite_state["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "CUBE.3mf"
        mon._notify_job_gcode["S001"] = "CUBE.3mf"
        mon._layer3_notified["S001"] = True
        mon._progress50_notified["S001"] = True
        mon._completed_notified["S001"] = True
        wecom._states["S001"].last_gcode = "CUBE.3mf"
        wecom._states["S001"].layer3_sent = True
        wecom._states["S001"].progress50_sent = True
        wecom._states["S001"].completed_sent = True

        # RUNNING -> FINISH（completed 已去重不入队）
        await mon._handle_state_change("S001", _make_status(
            "S001", PrinterState.FINISH, "CUBE.3mf",
            current_layer=100, total_layers=100, progress=100))
        assert wecom._queue.qsize() == 0

        # FINISH -> RUNNING 重打同名文件：确定状态为 FINISH，判为新任务重置两层标记
        await mon._handle_state_change("S001", _make_status(
            "S001", PrinterState.RUNNING, "CUBE.3mf",
            current_layer=0, total_layers=100, progress=0))
        assert mon._layer3_notified["S001"] is False
        assert wecom._states["S001"].layer3_sent is False

        # 到第3层触发通知
        mon._check_progress_events("S001", _make_status(
            "S001", PrinterState.RUNNING, "CUBE.3mf",
            current_layer=3, total_layers=100, progress=2))
        assert wecom._queue.qsize() == 1

    async def test_tc_notify_50_reconnect_same_job_no_duplicate(self, monitor_with_wecom, mock_wecom_notifier):
        """TC-NOTIFY-50: RUNNING→UNKNOWN→RUNNING 断线重连同一任务不重置、不重复通知"""
        mon = monitor_with_wecom
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_definite_state["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "CUBE.3mf"
        mon._notify_job_gcode["S001"] = "CUBE.3mf"
        mon._layer3_notified["S001"] = True
        mon._progress50_notified["S001"] = True

        # 断线：RUNNING -> UNKNOWN（确定状态仍为 RUNNING）
        await mon._handle_state_change("S001", _make_status(
            "S001", PrinterState.UNKNOWN, "CUBE.3mf",
            current_layer=50, total_layers=100, progress=50))
        # 重连：UNKNOWN -> RUNNING，同一任务中途，不应重置标记
        await mon._handle_state_change("S001", _make_status(
            "S001", PrinterState.RUNNING, "CUBE.3mf",
            current_layer=52, total_layers=100, progress=52))
        assert mon._layer3_notified["S001"] is True
        assert mon._progress50_notified["S001"] is True

        # 后续消息也不会补发
        mon._check_progress_events("S001", _make_status(
            "S001", PrinterState.RUNNING, "CUBE.3mf",
            current_layer=53, total_layers=100, progress=53))
        assert mock_wecom_notifier.notify.call_count == 0


# ---------------------------------------------------------------------------
# TC-NOTIFY-29~: HMS 错误通知测试
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHMSErrorNotification:
    async def test_tc_notify_29_hms_error_triggers_notification(self, monitor_with_wecom, mock_wecom_notifier):
        """TC-NOTIFY-29: HMS 错误触发 error 通知"""
        mon = monitor_with_wecom
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"

        hms_errors = [HMSError(code=0x0B01, severity="error", message="堵嘴检测")]
        s = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                         current_layer=50, total_layers=100, progress=50,
                         hms_errors=hms_errors)
        mon._check_progress_events("S001", s)

        error_calls = [c for c in mock_wecom_notifier.notify.call_args_list
                       if c[0][1] == EVENT_ERROR]
        assert len(error_calls) == 1

    async def test_tc_notify_30_same_hms_error_not_duplicated(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-30: 同一 HMS 错误不重复通知（去重逻辑在 wecom.notify() 中）"""
        _, calls = broadcast_calls
        from bumbo_stats.config import PrinterConfig
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        cfg = _make_wecom_config()
        cfg.layer3 = False  # 隔离 HMS 行为：layer>=3 会额外触发 layer3
        wecom = WeComNotifier(cfg, [printer_cfg])
        wecom._queue = asyncio.Queue(maxsize=10)
        wecom._chatid = "test_chatid"
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom)
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"

        hms_errors = [HMSError(code=0x0B01, severity="error", message="堵嘴检测")]

        # 第1次
        s1 = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                         current_layer=50, hms_errors=hms_errors)
        mon._check_progress_events("S001", s1)

        # 第2次（同样错误）- wecom.notify() 内部去重
        s2 = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                         current_layer=51, hms_errors=hms_errors)
        mon._check_progress_events("S001", s2)

        assert wecom._queue.qsize() == 1  # 不重复

    async def test_tc_notify_31_multiple_hms_errors_merged(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-31: 多个 HMS 错误合并为一条通知"""
        _, calls = broadcast_calls
        from bumbo_stats.config import PrinterConfig
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        cfg = _make_wecom_config()
        cfg.layer3 = False  # 隔离 HMS 行为：layer>=3 会额外触发 layer3
        wecom = WeComNotifier(cfg, [printer_cfg])
        wecom._queue = asyncio.Queue(maxsize=10)
        wecom._chatid = "test_chatid"
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom)
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"

        # 同时有堵头和缺料两个错误
        hms_errors = [
            HMSError(code=0x0B01, severity="error", message="堵嘴检测"),
            HMSError(code=0x0501, severity="error", message="缺料"),
        ]
        s = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                         current_layer=50, hms_errors=hms_errors)
        mon._check_progress_events("S001", s)

        assert wecom._queue.qsize() == 1
        item = await wecom._queue.get()
        assert item["event"] == EVENT_ERROR
        assert len(item["errors"]) == 2

    async def test_tc_notify_32_new_job_resets_hms_error(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-32: 新任务开始时 HMS 错误状态重置，可再次通知"""
        _, calls = broadcast_calls
        from bumbo_stats.config import PrinterConfig
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        cfg = _make_wecom_config()
        cfg.layer3 = False  # 隔离 HMS 行为：layer>=3 会额外触发 layer3
        wecom = WeComNotifier(cfg, [printer_cfg])
        wecom._queue = asyncio.Queue(maxsize=10)
        wecom._chatid = "test_chatid"
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom)
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job1.gcode"

        # job1 有错误
        hms_errors1 = [HMSError(code=0x0B01, severity="error", message="堵嘴检测")]
        s1 = _make_status("S001", PrinterState.RUNNING, "job1.gcode",
                         current_layer=50, hms_errors=hms_errors1)
        mon._check_progress_events("S001", s1)
        assert wecom._queue.qsize() == 1

        # job2 开始（gcode 变化）但无错误，不触发
        s2 = _make_status("S001", PrinterState.RUNNING, "job2.gcode",
                         current_layer=1, hms_errors=[])
        mon._check_progress_events("S001", s2)
        assert wecom._queue.qsize() == 1

        # job2 出现错误，能再次触发
        hms_errors2 = [HMSError(code=0x0501, severity="error", message="缺料")]
        s3 = _make_status("S001", PrinterState.RUNNING, "job2.gcode",
                         current_layer=10, hms_errors=hms_errors2)
        mon._check_progress_events("S001", s3)
        assert wecom._queue.qsize() == 2

    async def test_tc_notify_33_warning_severity_not_notified(self, monitor_with_wecom, mock_wecom_notifier):
        """TC-NOTIFY-33: severity=warning 的 HMS 条目不触发通知"""
        mon = monitor_with_wecom
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"

        # 只有 warning，没有 error
        hms_warnings = [HMSError(code=0x0901, severity="warning", message="摄像头离线")]
        s = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                         current_layer=50, hms_errors=hms_warnings)
        mon._check_progress_events("S001", s)

        error_calls = [c for c in mock_wecom_notifier.notify.call_args_list
                       if c[0][1] == EVENT_ERROR]
        assert len(error_calls) == 0

    async def test_tc_notify_34_hms_error_disabled_in_config(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-34: 配置中 error=false 时不发送错误通知"""
        _, calls = broadcast_calls
        from bumbo_stats.config import PrinterConfig
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        cfg = _make_wecom_config()
        cfg.error = False  # 禁用
        cfg.layer3 = False  # 隔离 HMS 行为：layer>=3 会额外触发 layer3
        wecom = WeComNotifier(cfg, [printer_cfg])
        wecom._queue = asyncio.Queue(maxsize=10)
        wecom._chatid = "test_chatid"
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom)
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job.gcode"

        hms_errors = [HMSError(code=0x0B01, severity="error", message="堵嘴检测")]
        s = _make_status("S001", PrinterState.RUNNING, "job.gcode",
                         current_layer=50, hms_errors=hms_errors)
        mon._check_progress_events("S001", s)

        assert wecom._queue.qsize() == 0

    async def test_tc_notify_35_multiple_printers_hms_independent(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-35: 多台打印机 HMS 错误独立追踪，互不干扰"""
        _, calls = broadcast_calls
        from bumbo_stats.config import PrinterConfig
        p1_cfg = PrinterConfig(serial="S001", name="P1")
        p2_cfg = PrinterConfig(serial="S002", name="P2")
        cfg = _make_wecom_config()
        cfg.layer3 = False  # 隔离 HMS 行为：layer>=3 会额外触发 layer3
        wecom = WeComNotifier(cfg, [p1_cfg, p2_cfg])
        wecom._queue = asyncio.Queue(maxsize=10)
        wecom._chatid = "test_chatid"
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom)
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job1.gcode"
        mon._last_states["S002"] = PrinterState.RUNNING
        mon._last_gcode["S002"] = "job2.gcode"

        # S001 有错误
        s1 = _make_status("S001", PrinterState.RUNNING, "job1.gcode",
                         current_layer=50, hms_errors=[
                             HMSError(code=0x0B01, severity="error", message="堵嘴检测"),
                         ])
        mon._check_progress_events("S001", s1)

        # S002 有不同错误
        s2 = _make_status("S002", PrinterState.RUNNING, "job2.gcode",
                         current_layer=50, hms_errors=[
                             HMSError(code=0x0501, severity="error", message="缺料"),
                         ])
        mon._check_progress_events("S002", s2)

        assert wecom._queue.qsize() == 2

    async def test_tc_notify_36_finish_triggers_completed_notification(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-36: RUNNING→FINISH 状态变更时触发 completed 通知

        修复场景：打印机从 RUNNING→FINISH 时，_check_progress_events 因 state!=RUNNING 跳过，
        因此 _handle_state_change 在检测到 FINISH 时直接触发 completed 通知。
        """
        _, calls = broadcast_calls
        from bumbo_stats.config import PrinterConfig
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        wecom = WeComNotifier(_make_wecom_config(), [printer_cfg])
        wecom._queue = asyncio.Queue(maxsize=10)
        wecom._chatid = "test_chatid"
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom)

        # 先设置初始状态 RUNNING（模拟正在打印）
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job1.gcode"

        # 状态变更为 FINISH（RUNNING→FINISH 触发 _handle_state_change 中的 completed 通知）
        s = _make_status("S001", PrinterState.FINISH, "job1.gcode",
                          current_layer=80, total_layers=80, progress=100)
        await mon._handle_state_change("S001", s)
        assert wecom._queue.qsize() == 1

    async def test_tc_notify_37_new_job_resets_completed_sent(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-37: 新打印任务开始时重置 completed_sent

        修复场景：job1.gcode 完成 → job2.gcode 开始，
        都需要能再次触发 completed 通知。
        """
        _, calls = broadcast_calls
        from bumbo_stats.config import PrinterConfig
        printer_cfg = PrinterConfig(serial="S001", name="P1")
        wecom = WeComNotifier(_make_wecom_config(), [printer_cfg])
        wecom._queue = asyncio.Queue(maxsize=10)
        wecom._chatid = "test_chatid"
        mon = PrintMonitor(mock_manager, mock_db, broadcast_calls[0], wecom)

        # job1 第一次完成
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job1.gcode"
        s1 = _make_status("S001", PrinterState.RUNNING, "job1.gcode",
                          current_layer=100, total_layers=100, progress=100)
        mon._check_progress_events("S001", s1)
        assert wecom._queue.qsize() == 1

        # job2 开始（RUNNING 状态变化，且 _handle_state_change 会重置 completed_sent）
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._last_gcode["S001"] = "job1.gcode"
        s2 = _make_status("S001", PrinterState.RUNNING, "job2.gcode",
                          current_layer=1, total_layers=100, progress=1)
        await mon._handle_state_change("S001", s2)

        # job2 完成，能再次触发 completed
        s3 = _make_status("S001", PrinterState.RUNNING, "job2.gcode",
                          current_layer=100, total_layers=100, progress=100)
        mon._check_progress_events("S001", s3)
        assert wecom._queue.qsize() == 2

    async def test_tc_notify_38_hms_errors_any_state_triggers(self, mock_manager, mock_db, broadcast_calls):
        """TC-NOTIFY-38: HMS 错误在任何状态都能触发（不限于 RUNNING）

        HMS 错误检测独立于 state!=RUNNING 的检查，任何状态下的错误都能被捕获。
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

        # IDLE 状态下有 HMS 错误，也能触发
        s = _make_status("S001", PrinterState.IDLE, "job.gcode",
                         current_layer=0, hms_errors=[
                             HMSError(code=0x0B01, severity="error", message="堵嘴检测"),
                         ])
        mon._check_progress_events("S001", s)
        assert wecom._queue.qsize() == 1
        item = await wecom._queue.get()
        assert item["event"] == EVENT_ERROR
