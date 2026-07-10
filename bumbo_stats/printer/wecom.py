"""企业微信智能机器人通知模块 — 在打印关键节点推送摄像头截图到群聊

触发事件:
  - layer3:    打印到达第 3 层（首层确认，打印正常进行中）
  - completed: 打印完成（进度 100% / 状态 FINISH）
  - error:     HMS 健康监控错误（堵头/卡料/缺料等）

架构:
  WeComNotifier 维护一个 asyncio.Queue 和一个后台任务。
  PrintMonitor 调用 notify() 将事件入队（非阻塞）。
  后台任务消费队列，抓取摄像头帧，上传并发送到企业微信群聊。
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from ..config import PrinterConfig

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

# 通知事件类型
EVENT_LAYER3 = "layer3"
EVENT_COMPLETED = "completed"
EVENT_ERROR = "error"

# 每次通知发送的截图数量和间隔（降低挤出机遮挡概率）
FRAMES_PER_NOTIFICATION = 2
FRAME_INTERVAL = 30.0  # 秒


@dataclass
class _PrinterNotifState:
    """单台打印机的通知状态追踪"""
    last_gcode: str = ""
    layer3_sent: bool = False
    completed_sent: bool = False
    last_error_snapshot: str = ""  # 上次通知的错误快照（防止同错误重复通知）

    def reset_if_new_job(self, gcode: str):
        """检测到新打印任务时重置通知状态"""
        if gcode and gcode != self.last_gcode:
            self.last_gcode = gcode
            self.layer3_sent = False
            self.completed_sent = False
            self.last_error_snapshot = ""  # 新任务开始时重置错误状态


@dataclass
class WeComConfig:
    """企业微信机器人配置"""
    bot_id: str = ""
    secret: str = ""
    chatid: str = ""           # 群聊 chatid，留空则自动发现
    layer3: bool = True        # 是否在第 3 层时发送
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
    ):
        self._cfg = wecom_config
        self._printer_names = {c.serial: c.name for c in printer_configs}
        self._camera = camera_manager

        # 通知队列
        self._queue: asyncio.Queue[dict] = asyncio.Queue()

        # 每台打印机的通知状态
        self._states: dict[str, _PrinterNotifState] = {
            c.serial: _PrinterNotifState() for c in printer_configs
        }

        # WeCom WebSocket 客户端
        self._client = None
        self._chatid: str = wecom_config.chatid
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def notify(self, serial: str, event: str, status=None, errors: list = None):
        """将通知事件加入队列（线程安全，非阻塞）

        Args:
            serial: 打印机序列号
            event: 事件类型 (EVENT_LAYER3 / EVENT_COMPLETED / EVENT_ERROR)
            status: 当前 PrinterStatus（可选，用于丰富消息内容）
            errors: 错误列表（仅 EVENT_ERROR 时使用）
        """
        state = self._states.get(serial)
        if not state:
            return

        # 去重检查
        gcode = getattr(status, "gcode_file", "") or ""
        state.reset_if_new_job(gcode)

        if event == EVENT_LAYER3:
            if state.layer3_sent or not self._cfg.layer3:
                return
            state.layer3_sent = True
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
        if not self._cfg.bot_id or not self._cfg.secret:
            logger.warning("企业微信 bot_id/secret 未配置，跳过通知模块")
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

    async def send_test_message(self) -> bool:
        """发送测试消息（包含所有打印机的摄像头截图）

        Returns:
            True if successful, False otherwise
        """
        if not self._cfg.bot_id or not self._cfg.secret:
            logger.warning("企业微信未配置 bot_id/secret")
            return False

        try:
            # 获取所有打印机的摄像头截图
            frames = []
            for serial, name in self._printer_names.items():
                frame = await self._capture_one(serial)
                if frame:
                    frames.append((name, frame))
                    logger.info("获取测试截图成功: %s", name)
                else:
                    logger.warning("获取测试截图失败: %s", name)

            # 构建测试消息
            from datetime import datetime
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            printer_list = "\n".join([f"- {name}" for name, _ in frames])

            markdown = f"""## 🧪 配置测试消息

> {now}

**发送者**: Bumbo Stats 监控面板

**测试打印机截图**:
{printer_list}

*如果看到此消息，说明企业微信配置成功！*"""

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

            # 发送文字消息
            await client.send_message(chatid, {
                "msgtype": "markdown",
                "markdown": {"content": markdown},
            })

            # 发送截图
            ts = int(time.time())
            for i, (name, frame) in enumerate(frames):
                media_id = await client.upload_media(
                    frame,
                    type="image",
                    filename=f"test_{ts}_{i+1}.jpg",
                )
                await client.send_media_message(
                    chatid,
                    media_type="image",
                    media_id=media_id["media_id"],
                )
                logger.info("测试截图发送成功: %s", name)

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

                # 注册消息回调以发现 chatid
                from wecom_aibot_sdk import MessageType
                self._client.on(MessageType.TEXT, self._on_chat_message)

                logger.info("正在连接企业微信 WebSocket...")
                await self._client.connect()
                await asyncio.sleep(2)  # 等待连接稳定
                logger.info("企业微信已连接")

                if not self._chatid:
                    logger.info(
                        "等待群聊 chatid：请在企业微信群中 @机器人 发一条消息..."
                    )

                # 持续处理通知队列
                while self._running:
                    try:
                        item = await asyncio.wait_for(
                            self._queue.get(), timeout=5.0
                        )
                        await self._process_notification(item)
                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        raise

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
        """收到消息时自动捕获 chatid（仅首次）"""
        if self._chatid:
            return
        body = msg.get("body", {})
        chatid = body.get("chatid", "")
        if chatid:
            self._chatid = chatid
            chat_type = body.get("chattype", "")
            logger.info("✅ 获取到群聊 chatid: %s (类型: %s)", chatid, chat_type)

    async def _process_notification(self, item: dict):
        """处理一条通知：抓帧 → 上传 → 发送"""
        serial = item["serial"]
        printer_name = item["printer_name"]
        event = item["event"]
        status = item.get("status")
        errors = item.get("errors")

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

        # HMS 错误通知不发截图，只发文字（错误信息已经包含关键视觉信息）
        frames = [] if event == EVENT_ERROR else await self._get_camera_frames(serial)

        # 构建 markdown 消息
        markdown = self._build_markdown(printer_name, event, status, errors)

        try:
            # 先发文字状态
            await self._client.send_message(chatid, {
                "msgtype": "markdown",
                "markdown": {"content": markdown},
            })

            # 再发送所有截图
            ts = int(time.time())
            for i, frame in enumerate(frames):
                if not self._client:
                    break
                upload_result = await self._client.upload_media(
                    frame,
                    type="image",
                    filename=f"{serial}_{event}_{ts}_{i+1}.jpg",
                )
                media_id = upload_result["media_id"]
                await self._client.send_media_message(
                    chatid,
                    media_type="image",
                    media_id=media_id,
                )

            if frames:
                logger.info(
                    "✅ 已发送监控截图: %s [%s] %d张",
                    printer_name, event, len(frames),
                )
            elif event != EVENT_ERROR:
                logger.warning("无摄像头帧，仅发送文字通知: %s", printer_name)

        except Exception as e:
            logger.error("企业微信发送失败: %s [%s] %s", printer_name, event, e)

    async def _get_camera_frames(self, serial: str) -> list[bytes]:
        """获取多张摄像头帧，间隔 FRAME_INTERVAL 秒，降低遮挡概率"""
        frames: list[bytes] = []
        for i in range(FRAMES_PER_NOTIFICATION):
            if i > 0:
                await asyncio.sleep(FRAME_INTERVAL)
            frame = await self._capture_one(serial)
            if frame:
                frames.append(frame)
        return frames

    async def _capture_one(self, serial: str) -> Optional[bytes]:
        """获取单张摄像头帧：优先从 CameraManager 缓存，失败则直连"""
        # 优先从缓存获取（低延迟）
        if self._camera:
            frame = await self._camera.get_frame(serial)
            if frame:
                return frame

        # 缓存没有，直连获取（高延迟，但确保拿到帧）
        config_ip = None
        config_code = None
        for pc in self._camera._configs.values() if self._camera else []:
            if pc.serial == serial:
                config_ip = pc.ip
                config_code = pc.access_code
                break

        if not config_ip or not config_code:
            return None

        try:
            def _capture():
                from bambulab import JPEGFrameStream
                s = JPEGFrameStream(config_ip, config_code)
                s.connect()
                f = s.get_frame()
                s.disconnect()
                return f
            frame = await asyncio.to_thread(_capture)
            return frame if isinstance(frame, (bytes, bytearray)) else None
        except Exception as e:
            logger.warning("直连摄像头失败 %s: %s", serial, e)
            return None

    @staticmethod
    def _build_markdown(printer_name: str, event: str, status, errors=None) -> str:
        """构建 Markdown 格式的通知消息

        Args:
            printer_name: 打印机名称
            event: 事件类型
            status: PrinterStatus
            errors: 错误列表（仅 EVENT_ERROR 时使用）
        """
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if event == EVENT_LAYER3:
            title = "🖨️ 打印已到达第 3 层"
        elif event == EVENT_COMPLETED:
            title = "✅ 打印已完成"
        elif event == EVENT_ERROR:
            title = "🚨 打印机异常"
        else:
            title = f"📢 {event}"

        gcode = getattr(status, "gcode_file", "") or ""
        layer = getattr(status, "current_layer", 0)
        total = getattr(status, "total_layers", 0)
        progress = getattr(status, "progress", 0)

        lines = [
            f"## {title}",
            f"> {now}",
            "",
            f"**打印机**: {printer_name}",
        ]
        if gcode:
            lines.append(f"**文件**: {gcode}")
        if total > 0:
            lines.append(f"**层数**: {layer}/{total}")
            lines.append(f"**进度**: {progress}%")

        # HMS 错误：使用注册表提供更友好的中文错误名和建议
        if event == EVENT_ERROR and errors:
            lines.append("")
            from ..models import lookup_hms_error
            for err in errors:
                info = lookup_hms_error(err.code)
                cat = info.get("category", "未知")
                suggestion = info.get("suggestion", "")
                # 优先用注册表中文名，其次用云端原始消息
                name = info.get("cn", err.message) if err.message else info.get("cn", "未知错误")
                lines.append(f"- **{name}** ({cat})")
                if suggestion:
                    lines.append(f"  💡 {suggestion}")
                # 保留原始信息便于排查
                lines.append(f"  📋 原始: {err.message or '(无)'}")
                lines.append(f"  🔢 码: 0x{err.code:08X}")

        return "\n".join(lines)
