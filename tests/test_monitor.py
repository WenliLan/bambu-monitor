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
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime

from bumbo_stats.models import PrinterState, PrinterStatus
from bumbo_stats.printer.monitor import PrintMonitor


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
