"""测试监控器状态检测逻辑 - printer/monitor.py

测试用例:
  TC-MON-01: 检测到打印开始（IDLE -> RUNNING）触发 record_print_start
  TC-MON-02: 检测到打印完成（RUNNING -> FINISH）触发 record_print_end
  TC-MON-03: 检测到打印失败（RUNNING -> FAILED）触发 record_print_end('failed')
  TC-MON-04: 状态不变时不触发任何事件
  TC-MON-05: handle_status_update 触发广播
  TC-MON-06: 首次消息 IDLE 仅初始化不触发事件
  TC-MON-07: 首次消息 RUNNING 触发补录
  TC-MON-08: gcode 文件变化触发结算旧任务+记录新任务
  TC-MON-09: gcode 不变时不触发事件
  TC-MON-10: RUNNING->IDLE 触发 record_print_end('completed')
  TC-MON-11: PAUSE->RUNNING 不触发新 session（保持打印连续性）
  TC-MON-12: gcode 为空时不触发 gcode 变化检测
  TC-MON-13: prev_gcode 为空时不触发 gcode 变化检测
  TC-MON-14: IDLE->FINISH 不触发任何事件
  TC-MON-15: 状态变更后更新 _last_gcode
  TC-MON-16: stop() 设置 _running = False
  TC-MON-17: _broadcast_all 广播所有打印机状态
  TC-MON-18: 首次消息 PAUSE 仅初始化
  TC-MON-19: RUNNING->PAUSE 不触发开始或结束
  TC-MON-20: 新任务开始重置 wecom / feishu 进度去重标记
  TC-MON-28: HMS severity=fatal 触发 _notify_all(error)
  TC-MON-29: HMS severity=serious/common/error/warning 均触发 _notify_all
  TC-MON-30: HMS severity 为空字符串 / 未知值不触发 _notify_all
  TC-MON-34: 启动扫描不设 wecom/feishu.completed_sent=True (历史 bug 修复)
  TC-MON-35: FINISH 路径触发完成通知前重置 wecom/feishu.completed_sent=False
  TC-MON-36: 99% 路径触发后 FINISH 路径去重不再重复发
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime

from bambu_monitor.models import PrinterState, PrinterStatus
from bambu_monitor.printer.monitor import PrintMonitor


def _make_status(serial, state, gcode_file="test.gcode"):
    return PrinterStatus(
        serial=serial, name="Test", state=state,
        gcode_file=gcode_file, last_updated=datetime.now(),
    )


@pytest.fixture
def monitor():
    mock_manager = MagicMock()
    mock_db = AsyncMock()
    broadcast_calls = []
    async def mock_broadcast(msg):
        broadcast_calls.append(msg)
    mon = PrintMonitor(mock_manager, mock_db, mock_broadcast)
    mon._broadcast_calls = broadcast_calls
    return mon


@pytest.fixture
def monitor_with_notifiers():
    """带 wecom / feishu mock notifier 的 monitor，用于测试重置逻辑"""
    mock_manager = MagicMock()
    mock_db = AsyncMock()
    broadcast_calls = []
    async def mock_broadcast(msg):
        broadcast_calls.append(msg)
    wecom = MagicMock()
    feishu = MagicMock()
    # 使用真实的 _PrinterNotifState dataclass，默认值是
    # last_gcode="" / layer3_sent=False / progress50_sent=False / completed_sent=False
    # 各测试在调用方法前可显式赋值模拟不同初始状态。
    from bambu_monitor.printer.wecom import _PrinterNotifState
    wecom_state = _PrinterNotifState()
    wecom._states = {"S001": wecom_state}
    feishu_state = _PrinterNotifState()
    feishu._states = {"S001": feishu_state}
    mon = PrintMonitor(mock_manager, mock_db, mock_broadcast,
                       wecom_notifier=wecom, feishu_notifier=feishu)
    mon._broadcast_calls = broadcast_calls
    return mon, wecom, wecom_state, feishu, feishu_state


@pytest.mark.asyncio
class TestStateChangeDetection:
    async def test_tc_mon_01_print_start(self, monitor):
        """TC-MON-01: IDLE->RUNNING 触发 record_print_start"""
        await monitor._handle_state_change("S001", _make_status("S001", PrinterState.RUNNING))
        monitor.db.record_print_start.assert_called_once_with("S001", "test.gcode")

    async def test_tc_mon_02_print_finish(self, monitor):
        """TC-MON-02: RUNNING->FINISH 触发 record_print_end('completed')"""
        monitor._last_states["S001"] = PrinterState.RUNNING
        await monitor._handle_state_change("S001", _make_status("S001", PrinterState.FINISH))
        monitor.db.record_print_end.assert_called_once_with("S001", "completed")

    async def test_tc_mon_03_print_failed(self, monitor):
        """TC-MON-03: RUNNING->FAILED 触发 record_print_end('failed')"""
        monitor._last_states["S001"] = PrinterState.RUNNING
        await monitor._handle_state_change("S001", _make_status("S001", PrinterState.FAILED))
        monitor.db.record_print_end.assert_called_once_with("S001", "failed")

    async def test_tc_mon_04_no_change_no_event(self, monitor):
        """TC-MON-04: 状态不变不触发事件"""
        monitor._last_states["S001"] = PrinterState.IDLE
        await monitor._handle_state_change("S001", _make_status("S001", PrinterState.IDLE))
        monitor.db.record_print_start.assert_not_called()
        monitor.db.record_print_end.assert_not_called()

    async def test_tc_mon_05_handle_status_update_broadcast(self, monitor):
        """TC-MON-05: handle_status_update 触发广播"""
        monitor.manager.get_all_statuses = MagicMock(return_value={
            "S001": _make_status("S001", PrinterState.IDLE),
        })
        await monitor.handle_status_update("S001", _make_status("S001", PrinterState.IDLE))
        # 广播被调用（事件驱动更新）
        assert len(monitor._broadcast_calls) == 1
        assert monitor._broadcast_calls[0]["type"] == "status_update"


@pytest.mark.asyncio
class TestFirstMessageGuard:
    async def test_tc_mon_06_first_idle_no_event(self, monitor):
        """TC-MON-06: 首次消息 IDLE 仅初始化不触发事件"""
        await monitor._handle_state_change("S001", _make_status("S001", PrinterState.IDLE))
        monitor.db.record_print_start.assert_not_called()
        monitor.db.record_print_end.assert_not_called()
        assert monitor._last_states["S001"] == PrinterState.IDLE

    async def test_tc_mon_07_first_running_records_start(self, monitor):
        """TC-MON-07: 首次消息 RUNNING 触发补录"""
        await monitor._handle_state_change("S001", _make_status("S001", PrinterState.RUNNING))
        monitor.db.record_print_start.assert_called_once_with("S001", "test.gcode")
        assert monitor._last_states["S001"] == PrinterState.RUNNING


@pytest.mark.asyncio
class TestGcodeChangeDetection:
    async def test_tc_mon_08_gcode_change_triggers_reset(self, monitor):
        """TC-MON-08: RUNNING->RUNNING 但 gcode 变化时结算旧任务+记录新任务"""
        monitor._last_states["S001"] = PrinterState.RUNNING
        monitor._last_gcode["S001"] = "old_file.gcode"
        await monitor._handle_state_change(
            "S001", _make_status("S001", PrinterState.RUNNING, gcode_file="new_file.gcode")
        )
        monitor.db.record_print_end.assert_called_once_with("S001", "completed")
        monitor.db.record_print_start.assert_called_once_with("S001", "new_file.gcode")

    async def test_tc_mon_09_same_gcode_no_trigger(self, monitor):
        """TC-MON-09: RUNNING->RUNNING 且 gcode 不变时不触发事件"""
        monitor._last_states["S001"] = PrinterState.RUNNING
        monitor._last_gcode["S001"] = "same.gcode"
        await monitor._handle_state_change(
            "S001", _make_status("S001", PrinterState.RUNNING, gcode_file="same.gcode")
        )
        monitor.db.record_print_start.assert_not_called()
        monitor.db.record_print_end.assert_not_called()


@pytest.mark.asyncio
class TestEdgeCaseTransitions:
    async def test_tc_mon_10_running_to_idle_completed(self, monitor):
        """TC-MON-10: RUNNING->IDLE 触发 record_print_end('completed')"""
        monitor._last_states["S001"] = PrinterState.RUNNING
        await monitor._handle_state_change("S001", _make_status("S001", PrinterState.IDLE))
        monitor.db.record_print_end.assert_called_once_with("S001", "completed")

    async def test_tc_mon_11_pause_to_running_no_trigger(self, monitor):
        """TC-MON-11: PAUSE->RUNNING 不触发新 session（保持同一打印任务连续性）

        需要预先设置 _pause_layer 为 >0（中途暂停恢复场景）。
        layer=0 时会被识别为新任务（PAUSE 是切片后等待开始的占位状态）。
        """
        monitor._last_states["S001"] = PrinterState.PAUSE
        monitor._last_gcode["S001"] = "paused.gcode"
        monitor._pause_layer["S001"] = 5  # 中途暂停
        await monitor._handle_state_change(
            "S001", _make_status("S001", PrinterState.RUNNING, gcode_file="paused.gcode")
        )
        monitor.db.record_print_start.assert_not_called()
        monitor.db.record_print_end.assert_not_called()

    async def test_tc_mon_12_gcode_change_empty_gcode_no_trigger(self, monitor):
        """TC-MON-12: gcode 为空时不触发 gcode 变化检测"""
        monitor._last_states["S001"] = PrinterState.RUNNING
        monitor._last_gcode["S001"] = "old_file.gcode"
        await monitor._handle_state_change(
            "S001", _make_status("S001", PrinterState.RUNNING, gcode_file="")
        )
        monitor.db.record_print_start.assert_not_called()
        monitor.db.record_print_end.assert_not_called()

    async def test_tc_mon_13_gcode_change_no_prev_gcode_no_trigger(self, monitor):
        """TC-MON-13: prev_gcode 为空时不触发 gcode 变化检测"""
        monitor._last_states["S001"] = PrinterState.RUNNING
        # _last_gcode 为空
        await monitor._handle_state_change(
            "S001", _make_status("S001", PrinterState.RUNNING, gcode_file="new.gcode")
        )
        monitor.db.record_print_start.assert_not_called()
        monitor.db.record_print_end.assert_not_called()

    async def test_tc_mon_14_idle_to_finish_no_event(self, monitor):
        """TC-MON-14: IDLE->FINISH 不触发任何事件"""
        monitor._last_states["S001"] = PrinterState.IDLE
        await monitor._handle_state_change("S001", _make_status("S001", PrinterState.FINISH))
        monitor.db.record_print_start.assert_not_called()
        monitor.db.record_print_end.assert_not_called()

    async def test_tc_mon_15_state_change_updates_last_gcode(self, monitor):
        """TC-MON-15: 状态变更后更新 _last_gcode"""
        monitor._last_states["S001"] = PrinterState.IDLE
        await monitor._handle_state_change(
            "S001", _make_status("S001", PrinterState.RUNNING, gcode_file="new.gcode")
        )
        assert monitor._last_gcode["S001"] == "new.gcode"
        assert monitor._last_states["S001"] == PrinterState.RUNNING

    async def test_tc_mon_16_stop_sets_running_false(self, monitor):
        """TC-MON-16: stop() 设置 _running = False"""
        monitor._running = True
        monitor.stop()
        assert monitor._running is False


@pytest.mark.asyncio
class TestBroadcast:
    async def test_tc_mon_17_broadcast_all(self, monitor):
        """TC-MON-17: _broadcast_all 广播所有打印机状态"""
        monitor.manager.get_all_statuses = MagicMock(return_value={
            "S001": _make_status("S001", PrinterState.IDLE),
            "S002": _make_status("S002", PrinterState.RUNNING),
        })
        await monitor._broadcast_all()
        assert len(monitor._broadcast_calls) == 1
        payload = monitor._broadcast_calls[0]
        assert payload["type"] == "status_batch"
        assert "S001" in payload["data"]
        assert "S002" in payload["data"]

    async def test_tc_mon_18_first_pause_only_init(self, monitor):
        """TC-MON-18: 首次消息 PAUSE 仅初始化不触发事件"""
        await monitor._handle_state_change("S001", _make_status("S001", PrinterState.PAUSE))
        monitor.db.record_print_start.assert_not_called()
        monitor.db.record_print_end.assert_not_called()
        assert monitor._last_states["S001"] == PrinterState.PAUSE

    async def test_tc_mon_19_running_to_pause_no_event(self, monitor):
        """TC-MON-19: RUNNING->PAUSE 不触发开始或结束"""
        monitor._last_states["S001"] = PrinterState.RUNNING
        await monitor._handle_state_change("S001", _make_status("S001", PrinterState.PAUSE))
        monitor.db.record_print_start.assert_not_called()
        monitor.db.record_print_end.assert_not_called()
        assert monitor._last_states["S001"] == PrinterState.PAUSE


@pytest.mark.asyncio
class TestStartLoop:
    async def test_tc_mon_20_start_and_cancel(self, monitor):
        """TC-MON-20: start() 启动循环并可被 cancel"""
        import asyncio
        monitor.manager.get_all_statuses = MagicMock(return_value={})
        # 启动循环并在 0.1s 后取消
        task = asyncio.create_task(monitor.start(broadcast_interval=0.05))
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # _broadcast_all 应该被调用过至少一次
        assert len(monitor._broadcast_calls) >= 1

    async def test_tc_mon_21_start_handles_broadcast_exception(self, monitor):
        """TC-MON-21: start() 循环中 broadcast 异常不崩溃"""
        import asyncio
        call_count = 0
        async def failing_broadcast(msg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("broadcast error")
        monitor.broadcast = failing_broadcast
        monitor.manager.get_all_statuses = MagicMock(return_value={})
        task = asyncio.create_task(monitor.start(broadcast_interval=0.05))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # 第一次异常后应该继续运行，第二次及以后正常
        assert call_count >= 2

    async def test_tc_mon_22_start_triggers_daily_snapshot(self, monitor):
        """TC-MON-22: start() 循环触发每日快照"""
        import asyncio
        monitor.manager.get_all_statuses = MagicMock(return_value={})
        task = asyncio.create_task(monitor.start(broadcast_interval=0.05))
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # snapshot_yesterday 应被调用过至少一次
        monitor.db.snapshot_yesterday.assert_called()

    async def test_tc_mon_23_startup_settles_orphan_with_heartbeat(self, monitor):
        """TC-MON-23: 启动扫描用心跳时刻结算孤儿 session（停机期不算打印时长）"""
        heartbeat = datetime(2026, 8, 8, 23, 27, 47)
        monitor.db.get_last_heartbeat = AsyncMock(return_value=heartbeat)
        monitor.manager.get_all_statuses = MagicMock(return_value={
            "S001": _make_status("S001", PrinterState.FINISH),
        })
        await monitor._scan_startup_notifications()
        monitor.db.record_print_end.assert_called_once_with(
            "S001", "completed", end_time=heartbeat
        )

    async def test_tc_mon_24_startup_settles_orphan_no_heartbeat(self, monitor):
        """TC-MON-24: 无心跳记录时退化为当前时间结算（end_time=None）"""
        monitor.db.get_last_heartbeat = AsyncMock(return_value=None)
        monitor.manager.get_all_statuses = MagicMock(return_value={
            "S001": _make_status("S001", PrinterState.FAILED),
        })
        await monitor._scan_startup_notifications()
        monitor.db.record_print_end.assert_called_once_with(
            "S001", "failed", end_time=None
        )

    async def test_tc_mon_25_start_writes_heartbeat(self, monitor):
        """TC-MON-25: start() 启动时立即写一次心跳"""
        import asyncio
        monitor.manager.get_all_statuses = MagicMock(return_value={})
        task = asyncio.create_task(monitor.start(broadcast_interval=0.05))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        monitor.db.update_heartbeat.assert_called()


@pytest.mark.asyncio
class TestNotifStateResetOnNewTask:
    """TC-MON-26~27: 新任务开始必须重置 wecom / feishu 状态标记

    历史 bug: feishu 状态重置时漏了 progress50_sent，
    导致重打同名文件后，飞书 50% 进度通知一直停留在“已发”状态而不触发。
    """

    async def test_tc_mon_26_feishu_resets_progress50_on_new_task(self, monitor_with_notifiers):
        """TC-MON-26: 终态→RUNNING 重置 feishu.progress50_sent=False

        场景: 上一任务发过 progress50，feishu.state.progress50_sent=True。
        打印机走完到 FINISH，状态变为 IDLE；再重打同一个 gcode，状态走回 RUNNING。
        预期: wecom 和 feishu 状态都重置，progress50_sent 重置为 False。
        """
        mon, wecom, wecom_state, feishu, feishu_state = monitor_with_notifiers
        # 模拟上一任务已发过 progress50 / completed
        wecom_state.layer3_sent = True
        wecom_state.progress50_sent = True
        wecom_state.completed_sent = True
        feishu_state.layer3_sent = True
        feishu_state.progress50_sent = True
        feishu_state.completed_sent = True
        feishu_state.last_error_snapshot = "prev"

        # 上一任务是 FINISH，进入新 RUNNING 任务（重打同名）
        mon._last_states["S001"] = PrinterState.FINISH
        mon._last_definite_state["S001"] = PrinterState.FINISH
        await mon._handle_state_change(
            "S001", _make_status("S001", PrinterState.RUNNING, gcode_file="test.gcode")
        )

        # monitor 自身状态已重置
        assert mon._progress50_notified["S001"] is False
        # wecom 完整重置
        assert wecom_state.progress50_sent is False
        assert wecom_state.layer3_sent is False
        assert wecom_state.completed_sent is False
        # 关键: feishu 也必须重置 progress50_sent
        assert feishu_state.progress50_sent is False, (
            "feishu.progress50_sent 未重置，新任务的 50% 进度通知会被跳"
            "过（feishu.notify 看到 progress50_sent=True 直接 return）"
        )
        assert feishu_state.layer3_sent is False
        assert feishu_state.completed_sent is False

    async def test_tc_mon_27_feishu_no_notifier_no_crash(self, monitor_with_notifiers):
        """TC-MON-27: feishu_notifier 缺失时不崩溃（仅 wecom 重置）"""
        mon, wecom, wecom_state, feishu, feishu_state = monitor_with_notifiers
        mon.feishu_notifier = None  # 移除 feishu notifier

        mon._last_states["S001"] = PrinterState.FINISH
        mon._last_definite_state["S001"] = PrinterState.FINISH
        # 不应抛异常
        await mon._handle_state_change(
            "S001", _make_status("S001", PrinterState.RUNNING, gcode_file="test.gcode")
        )
        # wecom 仍被重置
        assert wecom_state.progress50_sent is False


@pytest.mark.asyncio
class TestHMSErrorNotification:
    """TC-MON-28~32: _check_progress_events 中 HMS 严重级别过滤

    仅“严重级别”触发通知:
      fatal / serious / error -> 推送告警
      common / warning / 空 / 未知 -> 跳过（仅提示级别）

    历史 bug: 原过滤 `e.severity == "error"` 永远 False（manager.py
    未传 severity 字符串），导致 HMS 告警永远走不到飞书机器人。
    修复后: 兼容 fatal/serious/error 三个级别。
    """

    def _status_with_hms(self, severities):
        from bambu_monitor.models import HMSError
        status = _make_status("S001", PrinterState.RUNNING)
        status.hms_errors = [
            HMSError(
                code=0x03000200,
                severity=sev,
                message="nozzle temp",
                attr=0x03000200,
                raw_code=0x00010200,
            )
            for sev in severities
        ]
        return status

    async def test_tc_mon_28_fatal_triggers_notify(self, monitor):
        """TC-MON-28: HMS severity=fatal 触发 _notify_all('error', ...)"""
        from bambu_monitor.printer.wecom import EVENT_ERROR
        monitor.wecom_notifier = MagicMock()
        monitor._notify_all = MagicMock()
        status = self._status_with_hms(["fatal"])

        monitor._check_progress_events("S001", status)

        monitor._notify_all.assert_called_once()
        call_args = monitor._notify_all.call_args
        assert call_args[0][0] == "S001"
        assert call_args[0][1] == EVENT_ERROR
        # errors 列表包含该 fatal 错误
        errors_arg = call_args[1].get("errors") or call_args[0][3]
        assert len(errors_arg) == 1
        assert errors_arg[0].severity == "fatal"

    async def test_tc_mon_29_severe_levels_trigger_notify(self, monitor):
        """TC-MON-29: fatal/serious/error 三个严重级别都触发，common/warning 不触发"""
        # 避免 _check_progress_events 头部的“未配置任何 notifier”早退
        monitor.wecom_notifier = MagicMock()
        monitor._notify_all = MagicMock()
        # fatal/serious 触发，common/warning 不触发
        status = self._status_with_hms(
            ["fatal", "serious", "error", "common", "warning"]
        )
        monitor._check_progress_events("S001", status)

        assert monitor._notify_all.call_count == 1
        call_args = monitor._notify_all.call_args
        errors_arg = call_args[1].get("errors") or call_args[0][3]
        # 只有 3 个严重级别透传
        assert len(errors_arg) == 3
        sevs = sorted(e.severity for e in errors_arg)
        assert sevs == ["error", "fatal", "serious"]

    async def test_tc_mon_30_empty_unknown_severity_skipped(self, monitor):
        """TC-MON-30: severity 为空 / 未知值时不触发 _notify_all"""
        monitor.wecom_notifier = MagicMock()
        monitor._notify_all = MagicMock()
        # 空 / 未知 / None 都不应触发
        status = self._status_with_hms(["", "info", "debug", "  "])
        monitor._check_progress_events("S001", status)

        monitor._notify_all.assert_not_called()

    async def test_tc_mon_31_partial_mixed_severities(self, monitor):
        """TC-MON-31: 混合 severity，只推送严重级别项"""
        monitor.wecom_notifier = MagicMock()
        monitor._notify_all = MagicMock()
        # 3 个严重 + 2 个仅提示
        status = self._status_with_hms(
            ["fatal", "error", "common", "", "warning"]
        )
        monitor._check_progress_events("S001", status)

        assert monitor._notify_all.call_count == 1
        call_args = monitor._notify_all.call_args
        errors_arg = call_args[1].get("errors") or call_args[0][3]
        # 只有 2 个严重 severity 透传 (fatal + error，common/warning 不透传)
        assert len(errors_arg) == 2
        sevs = sorted(e.severity for e in errors_arg)
        assert sevs == ["error", "fatal"]

    async def test_tc_mon_32_no_hms_errors_no_notify(self, monitor):
        """TC-MON-32: status.hms_errors 为空时 _notify_all 不被调"""
        monitor.wecom_notifier = MagicMock()
        monitor._notify_all = MagicMock()
        status = _make_status("S001", PrinterState.RUNNING)
        status.hms_errors = []

        monitor._check_progress_events("S001", status)

        monitor._notify_all.assert_not_called()

    async def test_tc_mon_33_only_common_no_notify(self, monitor):
        """TC-MON-33: 仅 common 级别（蓝提示）时不触发告警"""
        monitor.wecom_notifier = MagicMock()
        monitor._notify_all = MagicMock()
        status = self._status_with_hms(["common"])
        monitor._check_progress_events("S001", status)

        # common 仅提示，不走告警
        monitor._notify_all.assert_not_called()


@pytest.mark.asyncio
class TestCompletedNotificationSendGuard:
    """TC-MON-34~36: 打印完成通知发送保证

    历史 bug：启动扫描 _scan_startup_notifications 把 wecom/feishu 的
    completed_sent 也设为 True，导致服务重启后即使打印机走完
    RUNNING→FINISH 状态变更，feishu.notify 的去重检查
    (state.completed_sent or not cfg.completed) 也会跳过入队，
    飞书/企微收不到完成通知。

    修复后：
    - 启动扫描不再设 wecom/feishu.completed_sent=True，仅设 layer3/progress50
    - FINISH 路径在 _notify_all 前显式重置 wecom/feishu.completed_sent=False
    - monitor._completed_notified 作为任务级去重屏障，99% 路径和 FINISH 路径
      只会有一个走 notify，另一个靠 _completed_notified=True 跳过
    """

    async def test_tc_mon_34_startup_scan_does_not_set_completed_sent(
        self, monitor_with_notifiers,
    ):
        """TC-MON-34: 启动扫描对 RUNNING 打印机不设 wecom/feishu.completed_sent=True

        验证修复 1：早期版本同时把 wecom/feishu 的 completed_sent 设了 True，
        导致服务重启后 RUNNING→FINISH 状态变更触发的完成通知被去重跳过。
        修复后启动扫描代码路径不应该包含 completed_sent = True 这一行，
        让 fixture 默认值（False）保持不变。
        """
        mon, wecom, wecom_state, feishu, feishu_state = monitor_with_notifiers

        # fixture 默认 completed_sent=False, 记录调用前的状态
        assert wecom_state.completed_sent is False
        assert feishu_state.completed_sent is False

        # mock manager 返回 RUNNING 状态的打印机
        mon.manager.get_all_statuses = MagicMock(return_value={
            "S001": _make_status("S001", PrinterState.RUNNING, gcode_file="job.gcode"),
        })
        # 无心跳记录
        mon.db.get_last_heartbeat = AsyncMock(return_value=None)

        await mon._scan_startup_notifications()

        # monitor 自身的 layer3/progress50 防重发（防中途误触发）
        assert mon._layer3_notified["S001"] is True
        assert mon._progress50_notified["S001"] is True
        # 关键：completed_notified 不设（保持 False），让后续 FINISH 路径能发送通知
        assert mon._completed_notified.get("S001", False) is False
        # wecom/feishu 的 completed_sent 不设（保持 fixture 默认 False）
        assert wecom_state.completed_sent is False, (
            "启动扫描不应修改 wecom.completed_sent，否则后续 "
            "RUNNING→FINISH 触发的完成通知会被 feishu.notify 去重跳过"
        )
        assert feishu_state.completed_sent is False, (
            "启动扫描不应修改 feishu.completed_sent，同上原因"
        )

    async def test_tc_mon_35_finish_resets_completed_sent_before_notify(
        self, monitor_with_notifiers,
    ):
        """TC-MON-35: RUNNING→FINISH 触发完成通知前重置 wecom/feishu.completed_sent

        验证修复 2：之前只重置 monitor 标记，不重置 wecom/feishu 标记。
        如果某种路径（如某种特殊初始化场景）导致 wecom/feishu.completed_sent
        被设为 True，FINISH 路径的 _notify_all 会因为去重跳过入队，通知丢失。
        """
        mon, wecom, wecom_state, feishu, feishu_state = monitor_with_notifiers
        # 模拟上一种状态：wecom/feishu.completed_sent 都被设为 True（残留状态）
        wecom_state.completed_sent = True
        feishu_state.completed_sent = True

        # 上一状态 RUNNING，现在到 FINISH
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._notify_all = MagicMock()

        await mon._handle_state_change(
            "S001", _make_status("S001", PrinterState.FINISH, gcode_file="job.gcode"),
        )

        # 关键：notify 必须被调用，且 called 前 wecom/feishu.completed_sent
        # 已被重置为 False，让 notify 内部能正常入队
        mon._notify_all.assert_called_once()
        # 检查调用前的状态
        assert wecom_state.completed_sent is False, (
            "FINISH 路径触发 _notify_all 前应重置 wecom.completed_sent"
        )
        assert feishu_state.completed_sent is False, (
            "FINISH 路径触发 _notify_all 前应重置 feishu.completed_sent"
        )
        # monitor._completed_notified 也设为 True（任务级去重，防止后续重复发）
        assert mon._completed_notified.get("S001", False) is True

    async def test_tc_mon_36_finish_after_99pct_dedup_skipped(
        self, monitor_with_notifiers,
    ):
        """TC-MON-36: 99% 路径已触发后 FINISH 路径不再重复发完成通知

        验证修复 3：避免 99% 和 FINISH 两条路径同时入队（双发）。
        monitor._completed_notified 作为任务级屏障，99% 路径触发后设 True，
        FINISH 路径检查到 True 后直接跳过。
        """
        mon, wecom, wecom_state, feishu, feishu_state = monitor_with_notifiers

        # 模拟 99% 路径已经触发了完成通知：monitor._completed_notified=True
        mon._completed_notified["S001"] = True
        wecom_state.completed_sent = True
        feishu_state.completed_sent = True

        # 上一状态 RUNNING，现在到 FINISH
        mon._last_states["S001"] = PrinterState.RUNNING
        mon._notify_all = MagicMock()

        await mon._handle_state_change(
            "S001", _make_status("S001", PrinterState.FINISH, gcode_file="job.gcode"),
        )

        # 99% 路径已发过，FINISH 路径必须跳过，避免重复发
        mon._notify_all.assert_not_called(), (
            "99% 路径已触发完成通知后，FINISH 路径必须去重跳过，"
            "否则会在同一任务内向飞书/企微重复发完成通知"
        )

