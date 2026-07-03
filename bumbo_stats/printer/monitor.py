"""后台监控任务 - 处理 MQTT 事件驱动的状态更新和定期批量广播"""

import asyncio
import logging
from datetime import date
from typing import Callable, Awaitable, Optional

from ..database import Database
from ..models import PrinterState, PrinterStatus
from .manager import PrinterManager

logger = logging.getLogger(__name__)

# 广播回调类型
BroadcastFn = Callable[[dict], Awaitable[None]]

# 默认批量广播间隔（秒）
DEFAULT_BROADCAST_INTERVAL = 5.0

# 触发通知的层数阈值
NOTIFY_LAYER = 3


class PrintMonitor:
    """后台打印状态监控器（事件驱动 + 定期广播）"""

    def __init__(
        self,
        manager: PrinterManager,
        db: Database,
        broadcast: BroadcastFn,
        wecom_notifier=None,
    ):
        self.manager = manager
        self.db = db
        self.broadcast = broadcast
        self.wecom_notifier = wecom_notifier  # 可选：企业微信通知器
        self._last_states: dict[str, PrinterState] = {}
        self._last_gcode: dict[str, str] = {}       # 上次处理时的 gcode（用于检测变化）
        self._prev_gcode: dict[str, str] = {}        # 更旧的上一次 gcode（用于比较）
        # 追踪每台打印机已触发的 layer3 通知（防止重复）
        self._layer3_notified: dict[str, bool] = {}
        self._running = False
        self._last_snapshot_date: date | None = date.today()

    async def start(self, broadcast_interval: float = DEFAULT_BROADCAST_INTERVAL):
        """启动定期批量广播循环

        此循环定期广播所有打印机的状态快照，确保 UI 一致性。
        实时的单台状态更新由 handle_status_update() 事件驱动。
        每日自动快照前一天的产能数据到 daily_usage。

        启动时还会扫描所有打印机当前状态，对已在打印中的任务补触发通知。

        Args:
            broadcast_interval: 批量广播间隔（秒）
        """
        self._running = True
        # 启动时补快照昨天（处理服务宕机期间遗漏的数据，已存在则跳过）
        await self.db.snapshot_yesterday()

        # 启动扫描：对已在打印中的任务补触发通知
        await self._scan_startup_notifications()

        logger.info("后台监控启动，批量广播间隔: %ss", broadcast_interval)
        while self._running:
            try:
                await asyncio.sleep(broadcast_interval)
                await self._broadcast_all()
                # 每日快照：仅在日期变更（午夜后）时触发，已存在则跳过
                today = date.today()
                if self._last_snapshot_date != today:
                    await self.db.snapshot_yesterday()
                    self._last_snapshot_date = today
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("监控批量广播异常: %s", e)

    async def _scan_startup_notifications(self):
        """启动时扫描所有打印机，初始化内部状态

        处理服务启动前就已经开始的打印任务，初始化 _last_gcode 和 _layer3_notified，
        避免启动后第一笔 MQTT 消息误判为 gcode 变化或触发重复通知。
        不发送任何通知（历史事件不补发）。
        """
        logger.info("[启动扫描] 开始扫描 %d 台打印机...", len(self.manager.get_all_statuses()))
        statuses = self.manager.get_all_statuses()
        for serial, status in statuses.items():
            # 跳过非运行状态
            if status.state != PrinterState.RUNNING:
                continue

            gcode = status.gcode_file or ""
            # 初始化 gcode 追踪
            self._last_gcode[serial] = gcode
            # 初始化 layer3 标记：已超第3层的设为 True，未到的设为 False
            if status.current_layer >= NOTIFY_LAYER:
                self._layer3_notified[serial] = True
            else:
                self._layer3_notified[serial] = False
            # 同步更新 WeComNotifier 的状态，防止 gcode 变化误判
            if self.wecom_notifier and serial in self.wecom_notifier._states:
                self.wecom_notifier._states[serial].last_gcode = gcode
                if status.current_layer >= NOTIFY_LAYER:
                    self.wecom_notifier._states[serial].layer3_sent = True
            logger.info(
                "[启动扫描] 初始化 %s: layer=%d/%d progress=%d%% gcode=%s layer3_notified=%s",
                serial, status.current_layer, status.total_layers,
                status.progress, gcode, self._layer3_notified.get(serial, False),
            )

    def stop(self):
        """停止监控"""
        self._running = False
        logger.info("后台监控已停止")

    async def handle_status_update(self, serial: str, status: PrinterStatus):
        """处理来自 MQTT 的实时状态更新（事件驱动）

        由 PrinterManager 的 MQTT 回调触发调用。

        Args:
            serial: 打印机序列号
            status: 最新的打印机状态
        """
        # 保存旧 gcode（用于 _check_progress_events 做变化检测）
        self._prev_gcode[serial] = self._last_gcode.get(serial, "")
        # 更新当前 gcode
        self._last_gcode[serial] = status.gcode_file or ""
        await self._handle_state_change(serial, status)
        self._check_progress_events(serial, status)

        # 广播单台打印机状态更新
        payload = {
            "type": "status_update",
            "data": {serial: status.model_dump(mode="json")},
        }
        await self.broadcast(payload)

    async def _broadcast_all(self):
        """广播所有打印机的当前状态快照"""
        statuses = self.manager.get_all_statuses()
        payload = {
            "type": "status_batch",
            "data": {s: st.model_dump(mode="json") for s, st in statuses.items()},
        }
        await self.broadcast(payload)

    def _check_progress_events(self, serial: str, status: PrinterStatus):
        """检查打印进度事件（第3层 / 100% 完成），触发企业微信通知

        Args:
            serial: 打印机序列号
            status: 最新状态
        """
        if not self.wecom_notifier:
            return

        # HMS 错误检测（任何状态都可能发生，与 RUNNING 独立）
        if status.hms_errors:
            error_events = [e for e in status.hms_errors if e.severity == "error"]
            if error_events:
                gcode = status.gcode_file or ""
                logger.info(
                    "触发HMS错误通知: %s errors=%s file=%s",
                    serial,
                    [(e.code, e.message) for e in error_events],
                    gcode,
                )
                self.wecom_notifier.notify(serial, "error", status, error_events)

        if status.state != PrinterState.RUNNING:
            return

        gcode = status.gcode_file or ""

        # 检测新任务，重置层3标记
        # 通过 handle_status_update 调用时：_prev_gcode 保存了更旧的值
        # 直接调用（如启动扫描）时：_prev_gcode 为空，fallback 到 _last_gcode
        prev_gcode = self._prev_gcode.get(serial, self._last_gcode.get(serial, ""))
        if gcode and gcode != prev_gcode:
            self._layer3_notified[serial] = False

        # 事件1：到达第3层（只有 current_layer==3 时触发一次）
        if (
            not self._layer3_notified.get(serial, False)
            and status.current_layer == NOTIFY_LAYER
        ):
            self._layer3_notified[serial] = True
            logger.info(
                "触发第3层通知: %s layer=%d/%d file=%s",
                serial, status.current_layer, status.total_layers, gcode,
            )
            self.wecom_notifier.notify(serial, "layer3", status)

        # 事件2：进度100%（只要 progress>=100 就触发）
        # 注意：RUNNING→FINISH 时 state 已变为 FINISH，_check_progress_events
        # 不会进入此分支，completed 通知由 _handle_state_change 在检测到 FINISH 时触发
        if status.progress >= 100:
            logger.info(
                "触发100%%通知: %s progress=%d file=%s",
                serial, status.progress, gcode,
            )
            self.wecom_notifier.notify(serial, "completed", status)

    async def _handle_state_change(self, serial: str, status: PrinterStatus):
        """处理状态变更事件（打印开始/结束检测）"""
        prev_state = self._last_states.get(serial)
        curr_state = status.state
        curr_gcode = status.gcode_file or ""

        if prev_state == curr_state:
            # 状态不变时：仍需更新 _last_gcode（支持同一状态下的 gcode 变化检测）
            prev_gcode = self._last_gcode.get(serial, "")
            self._last_gcode[serial] = curr_gcode
            # 检测 gcode_file 变化（断线期间完成旧任务又开始新任务）
            if curr_state == PrinterState.RUNNING:
                if prev_gcode and curr_gcode and prev_gcode != curr_gcode:
                    logger.info(
                        "gcode 变化 %s: %s -> %s (结算旧任务+记录新任务)",
                        serial, prev_gcode, curr_gcode,
                    )
                    await self.db.record_print_end(serial, "completed")
                    await self.db.record_print_start(serial, curr_gcode)
            return

        # 首次收到消息且无历史记录时，仅初始化状态
        if prev_state is None:
            logger.info("首次状态 %s: %s (仅初始化)", serial, curr_state.value)
            self._last_states[serial] = curr_state
            self._last_gcode[serial] = status.gcode_file
            if curr_state == PrinterState.RUNNING:
                await self.db.record_print_start(serial, status.gcode_file)
            return

        logger.info(
            "状态变更 %s: %s -> %s", serial,
            prev_state.value,
            curr_state.value,
        )

        # 检测打印开始（排除 PAUSE 恢复，避免分割同一打印任务）
        if curr_state == PrinterState.RUNNING and prev_state not in (
            PrinterState.RUNNING, PrinterState.PAUSE,
        ):
            await self.db.record_print_start(serial, status.gcode_file)
            # 重置 completed_sent，允许新任务再次触发 completed 通知
            if self.wecom_notifier and serial in self.wecom_notifier._states:
                self.wecom_notifier._states[serial].completed_sent = False

        # 检测打印结束（PAUSE 不结束 session，打印仍在进行中）
        elif prev_state == PrinterState.RUNNING and curr_state in (
            PrinterState.FINISH, PrinterState.FAILED, PrinterState.IDLE,
        ):
            end_status = "completed" if curr_state in (
                PrinterState.FINISH, PrinterState.IDLE,
            ) else "failed"
            await self.db.record_print_end(serial, end_status)
            # 触发 completed 通知（_check_progress_events 在 FINISH 时不会进入）
            if self.wecom_notifier and curr_state == PrinterState.FINISH:
                logger.info(
                    "触发完成通知: %s file=%s",
                    serial, status.gcode_file,
                )
                self.wecom_notifier.notify(serial, "completed", status)

        self._last_states[serial] = curr_state
        self._last_gcode[serial] = status.gcode_file
