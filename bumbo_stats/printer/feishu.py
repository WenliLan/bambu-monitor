"""飞书智能机器人通知模块 — 在打印关键节点推送摄像头截图到群聊

飞书机器人 API:
  1. 获取 tenant_access_token: POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
  2. 发送消息: POST https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id

触发事件:
  - layer3:    打印到达第 3 层（首层确认，打印正常进行中）
  - completed: 打印完成（进度 100% / 状态 FINISH）
  - error:     HMS 健康监控错误（堵头/卡料/缺料等）
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from ..config import PrinterConfig

logger = logging.getLogger(__name__)

# 静默第三方 SDK 日志
for _lib in ["lark", "urllib3"]:
    _l = logging.getLogger(_lib)
    _l.setLevel(logging.ERROR)
    _l.disabled = True

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
            self.last_error_snapshot = ""


@dataclass
class FeishuConfig:
    """飞书机器人配置"""
    app_id: str = ""
    app_secret: str = ""
    chatid: str = ""           # 群聊 chat_id，留空则使用配置的 chatid
    layer3: bool = True        # 是否在第 3 层时发送
    completed: bool = True     # 是否在打印完成时发送
    error: bool = True         # 是否在 HMS 错误时发送


class FeishuNotifier:
    """飞书机器人通知推送器

    使用方法:
        notifier = FeishuNotifier(config, printer_configs, camera_manager)
        await notifier.start()         # 启动后台任务
        notifier.notify(serial, ...)   # 入队通知（非阻塞）
        await notifier.stop()          # 停止
    """

    def __init__(
        self,
        feishu_config: FeishuConfig,
        printer_configs: list[PrinterConfig],
        camera_manager=None,
    ):
        self._cfg = feishu_config
        self._printer_names = {c.serial: c.name for c in printer_configs}
        self._camera = camera_manager

        # 通知队列
        self._queue: asyncio.Queue[dict] = asyncio.Queue()

        # 每台打印机的通知状态
        self._states: dict[str, _PrinterNotifState] = {
            c.serial: _PrinterNotifState() for c in printer_configs
        }

        # 飞书 API token
        self._tenant_token: Optional[str] = None
        self._token_expires_at: float = 0

        # 后台任务
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
                snapshot = json.dumps(
                    sorted((e.code, e.severity, e.message) for e in errors),
                )
                if state.last_error_snapshot == snapshot:
                    return
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
            logger.info("飞书通知入队: %s [%s] %s", printer_name, event, gcode)
        except asyncio.QueueFull:
            logger.warning("通知队列已满，丢弃: %s [%s]", printer_name, event)

    async def start(self):
        """启动后台飞书连接与通知处理任务"""
        if not self._cfg.app_id or not self._cfg.app_secret:
            logger.warning("飞书 app_id/app_secret 未配置，跳过通知模块")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("飞书通知模块已启动")

    async def stop(self):
        """停止后台任务并断开连接"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("飞书通知模块已停止")

    async def send_test_message(self) -> bool:
        """发送测试消息（包含所有打印机的摄像头截图）

        Returns:
            True if successful, False otherwise
        """
        if not self._cfg.app_id or not self._cfg.app_secret:
            logger.warning("飞书未配置 app_id/app_secret")
            return False

        try:
            # 获取 tenant_access_token
            await self._refresh_token()
            if not self._tenant_token:
                return False

            # 获取所有打印机的摄像头截图
            frames = []
            for serial, name in self._printer_names.items():
                frame = await self._capture_one(serial)
                if frame:
                    frames.append((name, frame))
                    logger.info("获取测试截图成功: %s", name)
                else:
                    logger.warning("获取测试截图失败: %s", name)

            if not frames:
                logger.error("没有可用的摄像头截图")
                return False

            # 构建测试消息
            from datetime import datetime
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            printer_list = "\n".join([f"- {name}" for name, _ in frames])

            content = f"""🧪 配置测试消息

{now}

发送者: Bumbo Stats 监控面板

测试打印机截图:
{printer_list}

如果看到此消息，说明飞书配置成功！"""

            # 发送消息
            await self._send_message(content, frames)
            logger.info("飞书测试消息发送完成")
            return True

        except Exception as e:
            logger.error("发送飞书测试消息失败: %s", e)
            return False

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _run_loop(self):
        """后台主循环：获取 token + 处理通知队列"""
        while self._running:
            try:
                # 获取 tenant_access_token
                await self._refresh_token()

                # 处理通知队列
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
                logger.error("飞书连接异常: %s，5s 后重连", e)
                await asyncio.sleep(5)

    async def _refresh_token(self):
        """获取飞书 tenant_access_token"""
        import aiohttp

        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self._cfg.app_id,
            "app_secret": self._cfg.app_secret,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    data = await resp.json()
                    if data.get("code") == 0:
                        self._tenant_token = data.get("tenant_access_token")
                        # token 有效期 2 小时，预留 10 分钟缓冲
                        self._token_expires_at = time.time() + (data.get("expire", 7200) - 600)
                        logger.info("飞书 token 获取成功")
                    else:
                        logger.error("飞书 token 获取失败: %s", data.get("msg"))
        except Exception as e:
            logger.error("获取飞书 token 失败: %s", e)

    async def _process_notification(self, item: dict):
        """处理一条通知：抓帧 → 上传 → 发送"""
        serial = item["serial"]
        printer_name = item["printer_name"]
        event = item["event"]
        status = item.get("status")
        errors = item.get("errors")

        # 确保有有效的 token
        if not self._tenant_token or time.time() >= self._token_expires_at:
            await self._refresh_token()
            if not self._tenant_token:
                logger.warning("飞书 token 不可用，跳过通知")
                return

        # HMS 错误通知不发截图，只发文字
        frames = [] if event == EVENT_ERROR else await self._get_camera_frames(serial)

        # 构建消息
        content = self._build_message(printer_name, event, status, errors)

        # 发送消息
        try:
            await self._send_message(content, frames)
            logger.info("✅ 飞书通知已发送: %s [%s]", printer_name, event)
        except Exception as e:
            logger.error("飞书发送失败: %s [%s] %s", printer_name, event, e)

    async def _send_message(self, content: str, frames: list):
        """发送飞书消息"""
        import aiohttp

        chat_id = self._cfg.chatid
        if not chat_id:
            logger.warning("飞书 chatid 未配置，跳过发送")
            return

        url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {
            "Authorization": f"Bearer {self._tenant_token}",
            "Content-Type": "application/json",
        }

        # 先发送文字消息
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": content}),
        }

        async with aiohttp.ClientSession() as session:
            await session.post(url, headers=headers, json=payload, timeout=10)

            # 再发送所有截图
            for i, frame in enumerate(frames):
                if not self._tenant_token:
                    break
                # 上传图片获取 media_id
                media_id = await self._upload_image(frame)
                if media_id:
                    image_payload = {
                        "receive_id": chat_id,
                        "msg_type": "image",
                        "content": json.dumps({"image_key": media_id}),
                    }
                    await session.post(url, headers=headers, json=image_payload, timeout=10)

    async def _upload_image(self, frame: bytes) -> Optional[str]:
        """上传图片到飞书，返回 media_id"""
        import aiohttp

        url = "https://open.feishu.cn/open-apis/im/v1/images"
        headers = {
            "Authorization": f"Bearer {self._tenant_token}",
        }

        form = aiohttp.FormData()
        form.add_field("image_type", "message")
        form.add_field("file", frame, filename="frame.jpg", content_type="image/jpeg")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, data=form, timeout=10) as resp:
                    data = await resp.json()
                    if data.get("code") == 0:
                        return data.get("data", {}).get("image_key")
        except Exception as e:
            logger.warning("飞书图片上传失败: %s", e)
        return None

    async def _get_camera_frames(self, serial: str) -> list:
        """获取多张摄像头帧"""
        frames: list = []
        for i in range(FRAMES_PER_NOTIFICATION):
            if i > 0:
                await asyncio.sleep(FRAME_INTERVAL)
            frame = await self._capture_one(serial)
            if frame:
                frames.append(frame)
        return frames

    async def _capture_one(self, serial: str) -> Optional[bytes]:
        """获取单张摄像头帧"""
        if self._camera:
            frame = await self._camera.get_frame(serial)
            if frame:
                return frame
        return None

    @staticmethod
    def _build_message(printer_name: str, event: str, status, errors=None) -> str:
        """构建通知消息"""
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
            f"{title}",
            f"> {now}",
            "",
            f"**打印机**: {printer_name}",
        ]
        if gcode:
            lines.append(f"**文件**: {gcode}")
        if total > 0:
            lines.append(f"**层数**: {layer}/{total}")
            lines.append(f"**进度**: {progress}%")

        if event == EVENT_ERROR and errors:
            lines.append("")
            from ..models import lookup_hms_error
            for err in errors:
                info = lookup_hms_error(err.code)
                cat = info.get("category", "未知")
                suggestion = info.get("suggestion", "")
                # 优先用注册表中文名，其次用云端原始消息
                name = info.get("cn", err.message) if err.message else info.get("cn", "未知错误")
                lines.append(f"- {name} ({cat})")
                if suggestion:
                    lines.append(f"  建议: {suggestion}")
                lines.append(f"  原始: {err.message or '(无)'} | 码: 0x{err.code:08X}")

        return "\n".join(lines)
