"""企业微信智能机器人通知模块 — 在打印关键节点推送摄像头截图到群聊

触发事件:
  - layer3:     打印到达第 3 层（首层确认，打印正常进行中）
  - progress50: 打印进度过半（50%）
  - completed:  打印完成（进度 >=99% / 状态 FINISH，99% 时截图能拍到成品）
  - error:      HMS 健康监控错误（堵头/卡料/缺料等）

架构:
  WeComNotifier 维护一个 asyncio.Queue 和一个后台任务。
  PrintMonitor 调用 notify() 将事件入队（非阻塞）。
  后台任务消费队列，抓取摄像头帧，上传并发送到企业微信群聊。

共享逻辑（指令解析 / 阈值 / 总览 HTML / 截图捕获等）从 .notify_common 复用。
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from ..config import PrinterConfig
from . import notify_common as nc

# 静默第三方 SDK 日志（禁用所有相关日志处理器）
for _lib in ["wecom_aibot_sdk", "websocket", "wsclient", "AiBotSDK"]:
    _l = logging.getLogger(_lib)
    _l.setLevel(logging.ERROR)
    _l.disabled = True
    # 移除所有处理器
    for _h in _l.handlers[:]:
        _l.removeHandler(_h)

logger = logging.getLogger(__name__)

# 静默第三方 SDK 的 DefaultLogger（直接用 print 输出）
class _SilentLogger:
    """静默的 logger，丢弃所有输出"""
    def debug(self, *args, **kwargs): pass
    def info(self, *args, **kwargs): pass
    def warn(self, *args, **kwargs): pass
    def error(self, *args, **kwargs): pass

# 通知事件类型（从 notify_common 复用，确保 wecom / feishu 一致）
EVENT_LAYER3 = nc.EVENT_LAYER3
EVENT_PROGRESS50 = nc.EVENT_PROGRESS50
EVENT_COMPLETED = nc.EVENT_COMPLETED
EVENT_ERROR = nc.EVENT_ERROR
EVENT_AI_ALARM = nc.EVENT_AI_ALARM

# 每次通知发送的截图数量和间隔（降低挤出机遮挡概率）
FRAMES_PER_NOTIFICATION = nc.FRAMES_PER_NOTIFICATION
FRAME_INTERVAL = nc.FRAME_INTERVAL

# 连接守护检查间隔（秒）：SDK 被服务端踢下线（同凭证新连接顶替）或
# 重连次数耗尽后会静默放弃，出站通知走 HTTP 不受影响，
# 但群指令（WebSocket 入站）会永久失效，需定期检查并重建连接
CONNECTION_CHECK_INTERVAL = 60.0

# 向后兼容：monitor.py / ai_guard.py 等仍 import 这些符号
normalize_gcode = nc.normalize_gcode


@dataclass
class _PrinterNotifState:
    """单台打印机的通知状态追踪"""
    last_gcode: str = ""
    layer3_sent: bool = False
    progress50_sent: bool = False
    completed_sent: bool = False
    last_error_snapshot: str = ""  # 上次通知的错误快照（防止同错误重复通知）

    def reset_if_new_job(self, gcode: str):
        """检测到新打印任务时重置通知状态（文件名归一化后比较）"""
        if not gcode:
            return
        if normalize_gcode(gcode) != normalize_gcode(self.last_gcode):
            self.last_gcode = gcode
            self.layer3_sent = False
            self.progress50_sent = False
            self.completed_sent = False
            self.last_error_snapshot = ""  # 新任务开始时重置错误状态
        else:
            # 同一任务但文件名形式不同（云端任务名 vs MQTT 文件名），同步为最新
            self.last_gcode = gcode


@dataclass
class WeComConfig:
    """企业微信机器人配置"""
    enabled: bool = True         # 总开关（与 WeComSettings.enabled 同步）
    bot_id: str = ""
    secret: str = ""
    chatid: str = ""           # 群聊 chatid，留空则自动发现
    layer3: bool = True        # 是否在第 3 层时发送
    progress50: bool = True    # 是否在进度 50% 时发送
    completed: bool = True     # 是否在打印完成时发送
    error: bool = True         # 是否在 HMS 错误时发送


class WeComNotifier:
    """企业微信通知推送器

    使用方法:
        notifier = WeComNotifier(config, printer_configs, camera_manager)
        await notifier.start()         # 启动后台任务
        notifier.notify(serial, ...)   # 入队通知（非阻塞）
        await notifier.stop()          # 停止
    """

    def __init__(
        self,
        wecom_config: WeComConfig,
        printer_configs: list[PrinterConfig],
        camera_manager=None,
        printer_manager=None,
        db=None,
        dashboard_url: str = "",
    ):
        self._cfg = wecom_config
        self._dashboard_url = dashboard_url  # 阈值响应结尾的面板链接（来自 settings.dashboard_url）
        self._printer_names = {c.serial: c.name for c in printer_configs}
        self._printer_locations = {c.serial: c.location or "" for c in printer_configs}
        self._camera = camera_manager
        self._manager = printer_manager  # 用于状态/总览指令获取实时状态
        self._db = db  # 用于总览指令获取产能数据

        # 通知队列
        self._queue: asyncio.Queue[dict] = asyncio.Queue()

        # 每台打印机的通知状态
        self._states: dict[str, _PrinterNotifState] = {
            c.serial: _PrinterNotifState() for c in printer_configs
        }

        # WeCom WebSocket 客户端
        self._client = None
        self._chatid: str = wecom_config.chatid
        self._ai_guard = None  # 由 bind_ai_guard() 注入（用于阈值指令）
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # 指令冷却控制（委托给 notify_common.CommandCooldown）
        self._cmd_cooldown = nc.CommandCooldown()

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def notify(self, serial: str, event: str, status=None, errors: list = None, ai_alert=None, alarm_frames: list = None):
        """将通知事件加入队列（线程安全，非阻塞）

        Args:
            serial: 打印机序列号
            event: 事件类型 (EVENT_LAYER3 / EVENT_PROGRESS50 / EVENT_COMPLETED / EVENT_ERROR / EVENT_AI_ALARM)
            status: 当前 PrinterStatus（可选，用于丰富消息内容）
            errors: 错误列表（仅 EVENT_ERROR 时使用）
            ai_alert: DetectionResult（仅 EVENT_AI_ALARM 时使用）
            alarm_frames: 触发报警的连续帧列表（仅 EVENT_AI_ALARM 时使用，直接发送无需重新抓帧）
        """
        state = self._states.get(serial)
        if not state:
            return

        # 总开关关闭时直接跳过（不入队，避免重连后补发历史事件）
        if not self._cfg.enabled:
            return

        # AI 报警事件：无去重逻辑（冷却由 AIGuardLoop 控制）
        if event == EVENT_AI_ALARM:
            if not self._cfg.error:  # 复用 error 开关控制报警类通知
                return
            printer_name = self._printer_names.get(serial, serial[-6:])
            try:
                self._queue.put_nowait({
                    "serial": serial,
                    "printer_name": printer_name,
                    "event": event,
                    "status": status,
                    "errors": errors,
                    "ai_alert": ai_alert,
                    "alarm_frames": alarm_frames,
                    "ts": time.time(),
                })
                logger.info("企业微信 AI 报警入队: %s [P=%.3f]", printer_name,
                            ai_alert.probability if ai_alert else 0)
            except asyncio.QueueFull:
                logger.warning("通知队列已满，丢弃: %s [ai_alarm]", printer_name)
            return

        # 去重检查
        gcode = getattr(status, "gcode_file", "") or ""
        state.reset_if_new_job(gcode)

        if event == EVENT_LAYER3:
            if state.layer3_sent or not self._cfg.layer3:
                return
            state.layer3_sent = True
        elif event == EVENT_PROGRESS50:
            if state.progress50_sent or not self._cfg.progress50:
                return
            state.progress50_sent = True
        elif event == EVENT_COMPLETED:
            if state.completed_sent or not self._cfg.completed:
                return
            state.completed_sent = True
        elif event == EVENT_ERROR:
            if not self._cfg.error:
                return
            # 合并多个错误为一条通知，通过快照比较防止重复
            if errors:
                # 用 (code, severity, message) 元组列表做快照，保证可排序
                snapshot = json.dumps(
                    sorted((e.code, e.severity, e.message) for e in errors),
                )
                if state.last_error_snapshot == snapshot:
                    return  # 错误完全相同，跳过
                state.last_error_snapshot = snapshot

        printer_name = self._printer_names.get(serial, serial[-6:])
        try:
            self._queue.put_nowait({
                "serial": serial,
                "printer_name": printer_name,
                "event": event,
                "status": status,
                "errors": errors,
                "ts": time.time(),
            })
            logger.info("企业微信通知入队: %s [%s] %s", printer_name, event, gcode)
        except asyncio.QueueFull:
            logger.warning("通知队列已满，丢弃: %s [%s]", printer_name, event)

    async def start(self):
        """启动后台 WeCom 连接与通知处理任务"""
        # 总开关关闭时不启动后台任务（避免空耗 WebSocket 连接）
        if not self._cfg.enabled:
            logger.info("企业微信通知已禁用（enabled=False），不启动后台任务")
            return
        if not self._cfg.bot_id or not self._cfg.secret:
            logger.warning("企业微信 bot_id/secret 未配置，跳过通知模块")
            return
        if self._running:
            logger.info("企业微信通知模块已在运行")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("企业微信通知模块已启动")

    async def stop(self):
        """停止后台任务并断开连接"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        logger.info("企业微信通知模块已停止")

    def update_config(self, new_cfg: WeComConfig) -> bool:
        """运行时更新配置（来自 /api/settings/wecom 重载）

        仅更新内存中的配置对象；底层聊天连接根据 enabled 状态同步：
        - enabled: True -> False：调用 stop() 释放 WebSocket 连接
        - enabled: False -> True：调用 start() 重建连接

        注意：本方法仅同步 _cfg 状态和切换动作，具体启停调用
        应在事件循环中由 _reload_wecom_impl 完成。
        """
        was_enabled = self._cfg.enabled
        self._cfg = new_cfg
        # 优先使用配置的 chatid，其次保持已自动发现的 chatid
        if new_cfg.chatid:
            self._chatid = new_cfg.chatid
        logger.info(
            "企业微信配置已重载: enabled=%s layer3=%s progress50=%s completed=%s error=%s",
            new_cfg.enabled, new_cfg.layer3, new_cfg.progress50,
            new_cfg.completed, new_cfg.error,
        )
        return was_enabled != new_cfg.enabled

    def bind_ai_guard(self, ai_guard) -> None:
        """注入 AI 检测循环引用（用于阈值指令调用 set_threshold）"""
        self._ai_guard = ai_guard

    async def send_test_message(self) -> bool:
        """发送测试消息：一条文字 + 仅第一台打印机的 1 张截图

        Returns:
            True if successful, False otherwise
        """
        if not self._cfg.bot_id or not self._cfg.secret:
            logger.warning("企业微信未配置 bot_id/secret")
            return False

        try:
            # 获取第一台可用的打印机截图（仅 1 张）
            from datetime import datetime
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            first_frame = None
            first_name = ""
            for serial, name in self._printer_names.items():
                frame = await self._capture_one(serial)
                if frame:
                    first_frame = frame
                    first_name = name
                    logger.info("获取测试截图成功: %s", name)
                    break
                else:
                    logger.warning("获取测试截图失败: %s", name)

            all_printers = "、".join(self._printer_names.values())
            markdown = f"""## 🧪 配置测试消息

> {now}

**发送者**: Bumbo Stats 监控面板

**打印机列表**: {all_printers}

*如果看到此消息和下面这张截图，说明企业微信配置成功！*"""

            # 优先使用配置的 chatid，否则等待自动发现的 chatid
            chatid = self._cfg.chatid or self._chatid
            if not chatid:
                logger.info("等待获取群聊 chatid...")
                for _ in range(30):  # 最多等 30 秒
                    await asyncio.sleep(1)
                    chatid = self._cfg.chatid or self._chatid
                    if chatid:
                        break
                if not chatid:
                    logger.error("chatid 获取超时")
                    return False

            # 连接并发送（使用当前配置新建独立连接，绕过旧后台连接）
            from wecom_aibot_sdk import WSClient

            client = WSClient(
                bot_id=self._cfg.bot_id,
                secret=self._cfg.secret,
                logger=_SilentLogger(),
            )
            await client.connect()
            await asyncio.sleep(2)

            # 1. 发文字（markdown）
            await client.send_message(chatid, {
                "msgtype": "markdown",
                "markdown": {"content": markdown},
            })

            # 2. 发 1 张截图（image）
            if first_frame:
                ts = int(time.time())
                try:
                    res = await client.upload_media(
                        first_frame,
                        type="image",
                        filename=f"test_{ts}.jpg",
                    )
                    await client.send_media_message(
                        chatid,
                        media_type="image",
                        media_id=res["media_id"],
                    )
                    logger.info("测试截图发送成功: %s", first_name)
                except Exception as e:
                    logger.warning("测试截图上传/发送失败: %s", e)
            else:
                logger.warning("无可用摄像头帧，仅发送文字测试消息")

            await client.disconnect()
            logger.info("企业微信测试消息发送完成")
            return True

        except Exception as e:
            logger.error("发送企业微信测试消息失败: %s", e)
            return False

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _run_loop(self):
        """后台主循环：连接 WeCom WebSocket + 处理通知队列"""
        from wecom_aibot_sdk import WSClient
        from wecom_aibot_sdk.logger import DefaultLogger
        
        while self._running:
            try:
                # 静默 WSClient 的日志输出
                self._client = WSClient(
                    bot_id=self._cfg.bot_id,
                    secret=self._cfg.secret,
                    logger=_SilentLogger(),
                )

                # 注册消息回调：发现 chatid + 响应截图指令
                # 注意：SDK 事件名为 "message"/"message.text"，
                # 不能用 MessageType 枚举值（"text"）注册，否则回调永不触发
                self._client.on("message", self._on_chat_message)

                logger.info("正在连接企业微信 WebSocket...")
                await self._client.connect()
                await asyncio.sleep(2)  # 等待连接稳定
                logger.info("企业微信已连接")

                if not self._chatid:
                    logger.info(
                        "等待群聊 chatid：请在企业微信群中 @机器人 发一条消息..."
                    )

                # 持续处理通知队列 + 定期检查 WebSocket 连接（守护入站指令通道）
                last_conn_check = time.time()
                while self._running:
                    try:
                        item = await asyncio.wait_for(
                            self._queue.get(), timeout=5.0
                        )
                        if item.get("event") == "snapshot":
                            await self._process_snapshot(item)
                        elif item.get("event") == "status":
                            await self._process_status(item)
                        elif item.get("event") == "overview":
                            await self._process_overview(item)
                        elif item.get("event") == "help":
                            await self._process_help(item)
                        elif item.get("event") == "threshold":
                            await self._process_threshold(item)
                        else:
                            await self._process_notification(item)
                    except asyncio.TimeoutError:
                        pass
                    except asyncio.CancelledError:
                        raise

                    # 连接守护：SDK 被踢下线/重连耗尽后不会自行恢复，
                    # 检测到断开则跳出内循环，由外层重建客户端
                    now = time.time()
                    if now - last_conn_check >= CONNECTION_CHECK_INTERVAL:
                        last_conn_check = now
                        if not self._client.is_connected:
                            logger.warning(
                                "企业微信 WebSocket 已断开（可能被同凭证新连接顶替"
                                "或重连耗尽），重建连接..."
                            )
                            break

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("企业微信连接异常: %s，5s 后重连", e)
                await asyncio.sleep(5)
            finally:
                if self._client:
                    try:
                        await self._client.disconnect()
                    except Exception:
                        pass
                    self._client = None

    async def _on_chat_message(self, msg):
        """收到消息：自动捕获 chatid + 响应截图指令"""
        body = msg.get("body", {})
        chatid = body.get("chatid", "")
        if chatid and not self._chatid:
            self._chatid = chatid
            chat_type = body.get("chattype", "")
            logger.info("✅ 获取到群聊 chatid: %s (类型: %s)", chatid, chat_type)

        # 指令解析：@机器人 发送含关键词的消息时触发对应指令
        text = self._extract_text(body)
        if not text:
            return
        parsed = nc.parse_command(text)
        if not parsed:
            return
        if not self._cmd_cooldown.check(parsed.command):
            return
        try:
            self._queue.put_nowait({
                "event": parsed.command, "ts": time.time(),
                "raw_text": parsed.raw_text,
                "numeric_value": parsed.numeric_value,
            })
            logger.info("收到指令 [%s]，已入队: %s", parsed.command, text.strip()[:50])
        except asyncio.QueueFull:
            logger.warning("通知队列已满，丢弃指令 [%s]", parsed.command)

    @staticmethod
    def _extract_text(body: dict) -> str:
        """从消息体中提取文本内容（兼容 text 和 mixed 类型）"""
        msgtype = body.get("msgtype", "")
        if msgtype == "text":
            return body.get("text", {}).get("content", "")
        if msgtype == "mixed":
            parts = []
            for item in body.get("mixed", {}).get("msg_item", []):
                if item.get("msgtype") == "text":
                    parts.append(item.get("text", {}).get("content", ""))
            return " ".join(parts)
        return ""

    async def _process_snapshot(self, item: dict):
        """响应截图指令：立即抓取所有打印机当前帧并发送"""
        chatid = self._cfg.chatid or self._chatid
        if not chatid:
            logger.warning("chatid 未就绪，跳过截图指令")
            return

        # 抓取每台打印机当前帧（单帧，优先缓存）
        frames: list[tuple[str, bytes]] = []
        missing: list[str] = []
        for serial, name in self._printer_names.items():
            frame = await self._capture_one(serial)
            if frame:
                frames.append((name, frame))
            else:
                missing.append(name)

        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = ["## 📷 实时监控截图", f"> {now}", ""]
        for name, _ in frames:
            lines.append(f"- {name} ✅")
        for name in missing:
            lines.append(f"- {name} ❌ 获取失败")

        try:
            await self._client.send_message(chatid, {
                "msgtype": "markdown",
                "markdown": {"content": "\n".join(lines)},
            })

            ts = int(time.time())
            for i, (name, frame) in enumerate(frames):
                if not self._client:
                    break
                upload_result = await self._client.upload_media(
                    frame,
                    type="image",
                    filename=f"snapshot_{ts}_{i+1}.jpg",
                )
                await self._client.send_media_message(
                    chatid,
                    media_type="image",
                    media_id=upload_result["media_id"],
                )

            logger.info("✅ 截图指令已响应: 成功 %d 台, 失败 %d 台", len(frames), len(missing))
        except Exception as e:
            logger.error("截图指令发送失败: %s", e)

    async def _process_status(self, item: dict):
        """响应状态指令：文字回复所有打印机当前状态"""
        chatid = self._cfg.chatid or self._chatid
        if not chatid:
            logger.warning("chatid 未就绪，跳过状态指令")
            return

        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = ["## 📊 打印机实时状态", f"> {now}", ""]

        if not self._manager:
            lines.append("⚠️ 状态数据不可用（manager 未接入）")
        else:
            statuses = self._manager.get_all_statuses()
            for serial, st in statuses.items():
                emoji = nc.state_emoji(st.state.value) or "❓"
                lines.append(f"**{emoji} {st.name}** ({st.state.value})")
                if st.gcode_file:
                    lines.append(f"- 文件: {st.gcode_file}")
                if st.total_layers > 0:
                    lines.append(f"- 层数: {st.current_layer}/{st.total_layers}  进度: {st.progress}%")
                if st.state.value == "RUNNING" and st.remaining_time > 0:
                    h, m = divmod(st.remaining_time, 60)
                    eta = f"{h}小时{m}分钟" if h else f"{m}分钟"
                    lines.append(f"- 预计剩余: {eta}")
                if st.nozzle_temp or st.bed_temp:
                    lines.append(f"- 喷嘴: {st.nozzle_temp:.0f}℃  热床: {st.bed_temp:.0f}℃")
                if st.hms_errors:
                    lines.append(f"- 🚨 异常: {len(st.hms_errors)} 个 HMS 错误")
                lines.append("")

        try:
            await self._client.send_message(chatid, {
                "msgtype": "markdown",
                "markdown": {"content": "\n".join(lines)},
            })
            logger.info("✅ 状态指令已响应")
        except Exception as e:
            logger.error("状态指令发送失败: %s", e)

    async def _process_help(self, item: dict):
        """响应帮助指令：列出所有支持的指令（文本来自 notify_common.HELP_TEXT）"""
        chatid = self._cfg.chatid or self._chatid
        if not chatid:
            logger.warning("chatid 未就绪，跳过帮助指令")
            return

        try:
            await self._client.send_message(chatid, {
                "msgtype": "markdown",
                "markdown": {"content": nc.HELP_TEXT},
            })
            logger.info("✅ 帮助指令已响应")
        except Exception as e:
            logger.error("帮助指令发送失败: %s", e)

    async def _process_threshold(self, item: dict):
        """响应阈值指令：动态修改 AI 异常检测阈值

        用法:
            阈值 0.6      -> 修改阈值为 0.6
            阈值          -> 仅查询当前阈值

        从 numeric_value 提取浮点数（0.0 ~ 1.0）。
        阈值修改会自动调用 AIGuardLoop.set_threshold，
        该方法会调用 on_threshold_changed 回调写回 printers.yaml，
        重启服务后仍然生效。
        """
        chatid = self._cfg.chatid or self._chatid
        if not chatid:
            logger.warning("chatid 未就绪，跳过阈值指令")
            return

        md = nc.build_threshold_message(
            self._ai_guard, item.get("numeric_value"),
            dashboard_url=self._dashboard_url,
        )

        try:
            await self._client.send_message(chatid, {
                "msgtype": "markdown",
                "markdown": {"content": md},
            })
            logger.info("✅ 阈值指令已响应")
        except Exception as e:
            logger.error("阈值指令发送失败: %s", e)

    @staticmethod
    async def _render_overview_image(html: str) -> Optional[bytes]:
        """用无头浏览器（系统 Edge 内核）将总览 HTML 渲染为长截图 JPEG

        委托给 notify_common.render_overview_image。
        渲染失败（未安装 playwright / 无 Edge）时返回 None，
        由调用方回退为 HTML 文件发送。
        """
        return await nc.render_overview_image(html)

    async def _process_overview(self, item: dict):
        """响应总览指令：生成与网页面板一致的长截图发送到群聊

        包含网页的4个区块：实时监控、打印机状态、今日产能饱和度、近30天产能趋势。
        优先渲染为长截图（图片消息，群里点开即看），
        渲染失败时回退为自包含 HTML 文件。
        """
        chatid = self._cfg.chatid or self._chatid
        if not chatid:
            logger.warning("chatid 未就绪，跳过总览指令")
            return

        # 抓取所有打印机当前帧和状态
        snapshots: dict[str, bytes] = {}  # serial -> JPEG bytes（委托 build_overview_html）
        for serial in self._printer_names:
            frame = await self._capture_one(serial)
            if frame:
                snapshots[serial] = frame

        statuses = self._manager.get_all_statuses() if self._manager else {}

        # 产能数据（与网页 /api/usage/today 和 /api/usage/monthly 同源）
        today_usage, monthly_usage = [], []
        if self._db and self._manager:
            try:
                today_usage = await self._db.get_today_usage_all(self._manager.printer_names)
                monthly_usage = await self._db.get_monthly_usage_all(
                    self._manager.printer_names, days=30
                )
            except Exception as e:
                logger.warning("获取产能数据失败: %s", e)

        html = nc.build_overview_html(
            self._printer_names, self._printer_locations,
            statuses, snapshots, today_usage, monthly_usage,
        )

        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 优先：渲染长截图以图片消息发送
        image = await self._render_overview_image(html)
        if image:
            try:
                upload_result = await self._client.upload_media(
                    image,
                    type="image",
                    filename=f"overview_{ts}.jpg",
                )
                await self._client.send_media_message(
                    chatid,
                    media_type="image",
                    media_id=upload_result["media_id"],
                )
                logger.info("✅ 总览指令已响应(长截图 %dKB): %d 台状态, %d 张截图, 产能条目 %d",
                            len(image) // 1024, len(statuses), len(snapshots), len(today_usage))
                return
            except Exception as e:
                logger.error("总览长截图发送失败: %s，回退为 HTML 文件", e)

        # 回退：以 HTML 文件发送
        try:
            upload_result = await self._client.upload_media(
                html.encode("utf-8"),
                type="file",
                filename=f"监控总览_{ts}.html",
            )
            await self._client.send_media_message(
                chatid,
                media_type="file",
                media_id=upload_result["media_id"],
            )
            logger.info("✅ 总览指令已响应(HTML文件): %d 台状态, %d 张截图, 产能条目 %d",
                        len(statuses), len(snapshots), len(today_usage))
        except Exception as e:
            logger.error("总览指令发送失败: %s", e)

    # ------------------------------------------------------------------
    # 总览 HTML 生成（已委托给 notify_common）
    # ------------------------------------------------------------------
    # 以下两个函数为向后兼容的薄包装：原代码若曾调用 WeComNotifier._build_monthly_svg /
    # _build_overview_html 会通过类查找但仍指向 nc 实现（避免修改调用点）。
    # 注：notify_common 已提供更完整的实现，推荐新代码直接调用 nc.build_overview_html。

    async def _process_notification(self, item: dict):
        """处理一条通知：发文字 + 1张截图（两条消息）"""
        serial = item["serial"]
        printer_name = item["printer_name"]
        event = item["event"]
        status = item.get("status")
        errors = item.get("errors")
        ai_alert = item.get("ai_alert")
        alarm_frames = item.get("alarm_frames")

        # 优先使用配置的 chatid，否则等待自动发现的 chatid
        chatid = self._cfg.chatid or self._chatid
        if not chatid:
            logger.info("chatid 未就绪，等待最多 60s...")
            for _ in range(60):
                chatid = self._cfg.chatid or self._chatid
                if chatid:
                    break
                await asyncio.sleep(1)
            if not chatid:
                logger.warning("chatid 超时，跳过本次通知")
                return

        # 截图获取策略：
        # - AI 报警：优先使用触发报警时缓存的帧（无需重新抓帧，无间隔延迟）
        # - HMS 错误：不发截图（错误信息已包含关键信息）
        # - 其他事件：实时抓 1 张帧
        if alarm_frames:
            frames = alarm_frames[:1]
        elif event == EVENT_ERROR:
            frames = []
        else:
            all_frames = await self._get_camera_frames(serial)
            frames = all_frames[:1] if all_frames else []

        # 上传 1 张截图到企微临时素材，返回 media_id
        media_id: Optional[str] = None
        if frames and self._client:
            ts = int(time.time())
            try:
                res = await self._client.upload_media(
                    frames[0], type="image", filename=f"{serial}_{event}_{ts}.jpg",
                )
                media_id = res["media_id"]
            except Exception as e:
                logger.warning("上传截图失败 %s [%s]: %s", printer_name, event, e)

        # 构建文字 markdown 消息
        md = self._build_markdown(
            printer_name, event, status, errors, ai_alert=ai_alert,
            dashboard_url=self._dashboard_url,
        )

        try:
            # 1. 发文字（markdown）
            await self._client.send_message(chatid, {
                "msgtype": "markdown",
                "markdown": {"content": md},
            })

            # 2. 发 1 张图片（image）
            if media_id:
                await self._client.send_media_message(
                    chatid,
                    media_type="image",
                    media_id=media_id,
                )
                logger.info(
                    "✅ 已发送文字+截图: %s [%s] (1张)",
                    printer_name, event,
                )
            elif event != EVENT_ERROR:
                logger.warning("无摄像头帧，仅发送文字: %s", printer_name)

        except Exception as e:
            logger.error("企业微信发送失败: %s [%s] %s", printer_name, event, e)

    async def _upload_images(self, frames: list[bytes], prefix: str) -> list[str]:
        """上传多张图片到企微临时素材，返回 media_id 列表

        现仅使用 1 张图（文字+1张图片消息组合），保留上传多张能力供未来扩展。
        """
        media_ids: list[str] = []
        if not self._client:
            return media_ids
        for i, frame in enumerate(frames[:1]):
            try:
                result = await self._client.upload_media(
                    frame, type="image", filename=f"{prefix}_{i+1}.jpg",
                )
                media_ids.append(result["media_id"])
            except Exception as e:
                logger.warning("上传截图 %s_%d 失败: %s", prefix, i + 1, e)
        return media_ids

    async def _get_camera_frames(self, serial: str) -> list[bytes]:
        """获取多张摄像头帧（委托给 notify_common）"""
        return await nc.capture_camera_frames(
            self._camera, serial,
            frames_per_notification=FRAMES_PER_NOTIFICATION,
            frame_interval=FRAME_INTERVAL,
        )

    async def _capture_one(self, serial: str) -> Optional[bytes]:
        """获取单张摄像头帧（委托给 notify_common）"""
        return await nc.capture_camera_one(self._camera, serial)

    @staticmethod
    def _build_markdown(
        printer_name: str, event: str, status, errors=None,
        ai_alert=None, dashboard_url: str = "",
    ) -> str:
        """构建 markdown 文字消息

        返回完整 markdown 文本（不含图片），由发送流程单独推送图片消息。

        事件类型:
            layer3/progress50/completed/error/ai_alarm
        """
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ---- 1. 标题（emoji + 事件类型）----
        if event == EVENT_LAYER3:
            title = "🖨️ 打印已到达第 3 层"
        elif event == EVENT_PROGRESS50:
            title = "⏳ 打印进度已过半"
        elif event == EVENT_COMPLETED:
            title = "✅ 打印已完成"
        elif event == EVENT_ERROR:
            title = "🚨 打印机异常"
        elif event == EVENT_AI_ALARM:
            title = "🤖 AI 检测到打印异常"
        else:
            title = f"📢 {event}"

        gcode = getattr(status, "gcode_file", "") or ""
        layer = getattr(status, "current_layer", 0)
        total = getattr(status, "total_layers", 0)
        progress = getattr(status, "progress", 0)
        st = getattr(status, "state", "") or ""

        # ---- 2. 正文 ----
        lines: list[str] = []
        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"> {now}")
        lines.append("")
        lines.append(f"**打印机**: {printer_name}")
        if gcode:
            lines.append(f"**文件**: {gcode}")

        if total > 0:
            lines.append(f"**层数**: {layer}/{total}")
            lines.append(f"**进度**: {progress}%")
        if st:
            emoji = nc.state_emoji(st) or ""
            if emoji:
                lines.append(f"**状态**: {emoji} {st}")
            else:
                lines.append(f"**状态**: {st}")

        if event == EVENT_AI_ALARM and ai_alert:
            lines.append("")
            lines.append(f"**异常概率**: {ai_alert.probability:.1%}")
            lines.append(f"**检测信息**: {ai_alert.message}")
            lines.append("")
            lines.append("💡 建议立即检查打印机，报警图片已保存至 `alerts_review/` 目录待审核")

        if event == EVENT_ERROR and errors:
            lines.append("")
            from ..models import lookup_hms_error
            # 告警级别 emoji: fatal/serious/common 来自 Bambu MQTT code>>16
            severity_emoji = {
                "fatal": "🔴", "serious": "🟡", "common": "🔵",
                "error": "🔴", "warning": "🟡",
            }
            for err in errors:
                info = lookup_hms_error(err.code)
                cat = info.get("category", "未知")
                suggestion = info.get("suggestion", "")
                name = info.get("cn", err.message) if err.message else info.get("cn", "未知错误")
                sev = (err.severity or "").strip().lower()
                sev_tag = f"{severity_emoji.get(sev, '⚪')}{sev or '未知'}"
                # 短码: attr 高 16 + raw_code 低 16, 形如 0300_0200
                short_code = f"{(err.attr >> 16) & 0xFFFF:04X}_{err.raw_code & 0xFFFF:04X}"
                lines.append(f"- **{name}** ({cat})")
                if suggestion:
                    lines.append(f"  - 💡 {suggestion}")
                lines.append(f"  - 📋 原始: {err.message or '(无)'}")
                lines.append(
                    f"  - 🔢 码: `0x{err.code:08X}`"
                    f" | 短码 `{short_code}` | 级别 {sev_tag}"
                )

        lines.append("")
        # 面板链接来自配置 settings.dashboard_url，留空则不输出（不再硬编码局域网 IP）
        if dashboard_url:
            lines.append(f"[打开监控面板]({dashboard_url})")

        return "\n".join(lines)
