"""后台监控任务 - 处理 MQTT 事件驱动的状态更新和定期批量广播"""

import asyncio
import logging
from datetime import date
from typing import Callable, Awaitable, Optional

from ..database import Database
from ..models import PrinterState, PrinterStatus
from .manager import PrinterManager
from .wecom import normalize_gcode

logger = logging.getLogger(__name__)

# 广播回调类型
BroadcastFn = Callable[[dict], Awaitable[None]]

# 默认批量广播间隔（秒）
DEFAULT_BROADCAST_INTERVAL = 5.0

# 触发通知的层数阈值
NOTIFY_LAYER = 3

# 触发通知的进度阈值（百分比）
NOTIFY_PROGRESS = 50

# 触发完成通知的进度阈值（99% 时打印头尚未归位，截图能拍到成品）
NOTIFY_COMPLETE_PROGRESS = 99


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
        # 最后一次确定状态（跳过 UNKNOWN/OFFLINE，用于区分断线重连和真正的新任务）
        self._last_definite_state: dict[str, PrinterState] = {}
        # 当前通知周期对应的任务文件（用于检测新任务重置通知标记）
        self._notify_job_gcode: dict[str, str] = {}
        # 追踪每台打印机已触发的 layer3 通知（防止重复）
        self._layer3_notified: dict[str, bool] = {}
        # 追踪每台打印机已触发的 50% 进度通知（防止重复）
        self._progress50_notified: dict[str, bool] = {}
        # 追踪每台打印机已触发的完成通知（防止 99% 后每条消息重复入队/刷日志）
        self._completed_notified: dict[str, bool] = {}
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
            # 非运行态：结算残留的孤儿 running session（服务停机期间
            # 打完/失败的任务没机会走状态变更结算，会被产能计算一直累加）
            if status.state != PrinterState.RUNNING:
                if status.state in (
                    PrinterState.FINISH, PrinterState.FAILED, PrinterState.IDLE,
                ):
                    end_status = (
                        "failed" if status.state == PrinterState.FAILED
                        else "completed"
                    )
                    await self.db.record_print_end(serial, end_status)
                continue

            gcode = status.gcode_file or ""
            # 初始化 gcode 追踪
            self._last_gcode[serial] = gcode
            self._notify_job_gcode[serial] = gcode
            # 运行中任务一律标记已发（历史事件不补发）：
            # 启动时 MQTT 数据可能不完整（打印中却报 layer=0/0），
            # 若据此设为 False，后续首条完整消息会在中途层数误发通知
            self._layer3_notified[serial] = True
            self._progress50_notified[serial] = True
            self._completed_notified[serial] = True
            # 同步更新 WeComNotifier 的状态，防止 gcode 变化误判
            if self.wecom_notifier and serial in self.wecom_notifier._states:
                self.wecom_notifier._states[serial].last_gcode = gcode
                self.wecom_notifier._states[serial].layer3_sent = True
                self.wecom_notifier._states[serial].progress50_sent = True
                self.wecom_notifier._states[serial].completed_sent = True
            logger.info(
                "[启动扫描] 初始化 %s: layer=%d/%d progress=%d%% gcode=%s (运行中任务不补发通知)",
                serial, status.current_layer, status.total_layers,
                status.progress, gcode,
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
        """检查打印进度事件（第3层 / 50%进度 / 100% 完成），触发企业微信通知

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

        # 检测新任务，重置通知标记：与当前通知周期的任务比较。
        # 不能逐条消息比较——任务切换往往发生在 FINISH/UNKNOWN 窗口，
        # 等状态回到 RUNNING 时前后两条消息的 gcode 已看不到变化
        # 归一化后比较：云端任务名不含扩展名，MQTT 上报含 .3mf，避免误判新任务
        job_gcode = self._notify_job_gcode.get(serial, "")
        if gcode and normalize_gcode(gcode) != normalize_gcode(job_gcode):
            self._notify_job_gcode[serial] = gcode
            self._layer3_notified[serial] = False
            self._progress50_notified[serial] = False
            self._completed_notified[serial] = False

        # 事件1：到达第3层（>= 判断，MQTT 跳层上报也不会错过；标记防止重复；
        # progress<99 避免完成时补发 layer3）
        if (
            not self._layer3_notified.get(serial, False)
            and status.current_layer >= NOTIFY_LAYER
            and status.progress < NOTIFY_COMPLETE_PROGRESS
        ):
            self._layer3_notified[serial] = True
            logger.info(
                "触发第3层通知: %s layer=%d/%d file=%s",
                serial, status.current_layer, status.total_layers, gcode,
            )
            self.wecom_notifier.notify(serial, "layer3", status)

        # 事件2：进度过半（>=50%，未到完成阈值时触发一次）
        if (
            not self._progress50_notified.get(serial, False)
            and NOTIFY_PROGRESS <= status.progress < NOTIFY_COMPLETE_PROGRESS
        ):
            self._progress50_notified[serial] = True
            logger.info(
                "触发50%%进度通知: %s progress=%d layer=%d/%d file=%s",
                serial, status.progress, status.current_layer, status.total_layers, gcode,
            )
            self.wecom_notifier.notify(serial, "progress50", status)

        # 事件3：进度达到完成阈值（99%，此时截图能拍到成品；标记防止
        # 进度停在 99% 期间每条消息重复入队/刷日志）
        # 注意：RUNNING→FINISH 时 state 已变为 FINISH，_check_progress_events
        # 不会进入此分支，完成通知由 _handle_state_change 在检测到 FINISH 时兜底触发
        if (
            not self._completed_notified.get(serial, False)
            and status.progress >= NOTIFY_COMPLETE_PROGRESS
        ):
            self._completed_notified[serial] = True
            logger.info(
                "触发完成通知(>=99%%): %s progress=%d file=%s",
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
            self._update_definite_state(serial, curr_state)
            # 检测 gcode_file 变化（断线期间完成旧任务又开始新任务），归一化后比较
            if curr_state == PrinterState.RUNNING:
                if (prev_gcode and curr_gcode
                        and normalize_gcode(prev_gcode) != normalize_gcode(curr_gcode)):
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
            self._update_definite_state(serial, curr_state)
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
            # 区分真正的新任务和断线重连：上一个确定状态是 FINISH/FAILED/IDLE
            # 说明旧任务已结束，本次 RUNNING 是新任务（即使重打同名文件也要重置通知）；
            # 若确定状态是 RUNNING（如 RUNNING→UNKNOWN→RUNNING 断线重连）则不重置，
            # 避免同一任务中途重复通知
            definite = self._last_definite_state.get(serial)
            if definite in (
                PrinterState.FINISH, PrinterState.FAILED, PrinterState.IDLE,
            ):
                logger.info(
                    "新任务开始，重置通知标记: %s file=%s", serial, curr_gcode,
                )
                self._notify_job_gcode[serial] = curr_gcode
                self._layer3_notified[serial] = False
                self._progress50_notified[serial] = False
                self._completed_notified[serial] = False
                # 同步重置 wecom 层去重标记（重打同名文件时 reset_if_new_job 不会重置）
                if self.wecom_notifier and serial in self.wecom_notifier._states:
                    st = self.wecom_notifier._states[serial]
                    st.last_gcode = curr_gcode
                    st.layer3_sent = False
                    st.progress50_sent = False
                    st.completed_sent = False

        # 检测打印结束（RUNNING 或 PAUSE 进入终态都要结算：
        # 暂停中取消/失败走 PAUSE->FAILED，不结算会留下永久 running 的孤儿 session，
        # 产能饱和度会把它一直算到当前时刻）
        elif prev_state in (
            PrinterState.RUNNING, PrinterState.PAUSE,
        ) and curr_state in (
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
        self._update_definite_state(serial, curr_state)

    def _update_definite_state(self, serial: str, state: PrinterState):
        """记录最后一次确定状态（跳过 UNKNOWN/OFFLINE 这类断线中间态）"""
        if state not in (PrinterState.UNKNOWN, PrinterState.OFFLINE):
            self._last_definite_state[serial] = state
