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
EVENT_PROGRESS50 = "progress50"
EVENT_COMPLETED = "completed"
EVENT_ERROR = "error"

# 每次通知发送的截图数量和间隔（降低挤出机遮挡概率）
FRAMES_PER_NOTIFICATION = 2
FRAME_INTERVAL = 30.0  # 秒

# 群聊指令关键词与冷却时间（秒），按顺序匹配，命中即停
SNAPSHOT_KEYWORDS = ("截图", "监控")
COMMANDS = (
    ("help", ("帮助", "help")),
    ("overview", ("总览",)),
    ("status", ("状态",)),
    ("snapshot", SNAPSHOT_KEYWORDS),
)
COMMAND_COOLDOWN = {"snapshot": 10.0, "overview": 10.0, "status": 5.0, "help": 3.0}

# 状态对应的图标
_STATE_EMOJI = {
    "RUNNING": "🟢",
    "IDLE": "⚪",
    "PAUSE": "⏸️",
    "FINISH": "✅",
    "FAILED": "❌",
    "OFFLINE": "⚫",
    "UNKNOWN": "❓",
}


def normalize_gcode(name: str) -> str:
    """归一化 gcode 文件名用于任务比较

    云端 API 任务名不含扩展名（如 "CUBE_2.4 wide"），
    MQTT 上报为文件名（如 "CUBE_2.4 wide.3mf"），
    直接比较会误判为新任务导致重复通知/任务误分割。
    """
    n = (name or "").strip()
    changed = True
    while changed:
        changed = False
        for ext in (".3mf", ".gcode"):
            if n.lower().endswith(ext):
                n = n[: -len(ext)]
                changed = True
    return n


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
    ):
        self._cfg = wecom_config
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
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_cmd_ts: dict[str, float] = {}  # 各指令上次触发时间

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def notify(self, serial: str, event: str, status=None, errors: list = None):
        """将通知事件加入队列（线程安全，非阻塞）

        Args:
            serial: 打印机序列号
            event: 事件类型 (EVENT_LAYER3 / EVENT_PROGRESS50 / EVENT_COMPLETED / EVENT_ERROR)
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

                # 持续处理通知队列
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
                        else:
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
        for cmd, keywords in COMMANDS:
            if any(kw in text for kw in keywords):
                now = time.time()
                cooldown = COMMAND_COOLDOWN.get(cmd, 5.0)
                if now - self._last_cmd_ts.get(cmd, 0.0) < cooldown:
                    logger.info("指令 [%s] 冷却中，忽略: %s", cmd, text.strip()[:50])
                    return
                self._last_cmd_ts[cmd] = now
                try:
                    self._queue.put_nowait({"event": cmd, "ts": now})
                    logger.info("收到指令 [%s]，已入队: %s", cmd, text.strip()[:50])
                except asyncio.QueueFull:
                    logger.warning("通知队列已满，丢弃指令 [%s]", cmd)
                return

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
                emoji = _STATE_EMOJI.get(st.state.value, "❓")
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
        """响应帮助指令：列出所有支持的指令"""
        chatid = self._cfg.chatid or self._chatid
        if not chatid:
            logger.warning("chatid 未就绪，跳过帮助指令")
            return

        markdown = """## 🤖 支持的指令

@机器人 发送含以下关键词的消息即可触发：

- **截图** / **监控** — 发送所有打印机实时摄像头截图
- **状态** — 文字回复所有打印机当前状态（层数/进度/剩余时间/温度）
- **总览** — 生成监控面板完整长截图（含状态+截图+产能图表）
- **帮助** — 显示本消息

另外会自动推送：打印到第 3 层、打印完成、HMS 异常告警"""

        try:
            await self._client.send_message(chatid, {
                "msgtype": "markdown",
                "markdown": {"content": markdown},
            })
            logger.info("✅ 帮助指令已响应")
        except Exception as e:
            logger.error("帮助指令发送失败: %s", e)

    @staticmethod
    async def _render_overview_image(html: str) -> Optional[bytes]:
        """用无头浏览器（系统 Edge 内核）将总览 HTML 渲染为长截图 JPEG

        渲染失败（未安装 playwright / 无 Edge）时返回 None，
        由调用方回退为 HTML 文件发送。
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("playwright 未安装，总览回退为 HTML 文件发送")
            return None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(channel="msedge", headless=True)
                try:
                    page = await browser.new_page(
                        viewport={"width": 760, "height": 600},
                        device_scale_factor=2,  # 2x 渲染，手机上放大看清晰
                    )
                    await page.set_content(html, wait_until="load")
                    # 企业微信图片消息限制 2MB，超限时逐档降低质量
                    img = b""
                    for quality in (80, 60, 40):
                        img = await page.screenshot(
                            full_page=True, type="jpeg", quality=quality
                        )
                        if len(img) <= 1_900_000:
                            break
                    return img
                finally:
                    await browser.close()
        except Exception as e:
            logger.warning("总览长截图渲染失败: %s，回退为 HTML 文件发送", e)
            return None

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
        import base64
        snapshots: dict[str, str] = {}  # serial -> base64 jpeg
        for serial in self._printer_names:
            frame = await self._capture_one(serial)
            if frame:
                snapshots[serial] = base64.b64encode(frame).decode()

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

        html = self._build_overview_html(statuses, snapshots, today_usage, monthly_usage)

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

    @staticmethod
    def _build_monthly_svg(monthly_usage: list) -> str:
        """用 SVG 绘制近30天产能饱和度折线图（自包含，无需 JS）"""
        if not monthly_usage or not any(m.points for m in monthly_usage):
            return '<div class="no-img">暂无产能数据</div>'

        colors = ["#10b981", "#3b82f6", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899"]
        dates = sorted({p.date for m in monthly_usage for p in m.points})
        n = len(dates)
        max_pct = max((p.utilization_percent for m in monthly_usage for p in m.points), default=0)
        # y 轴上限：向上取整到 25 的倍数，至少 25，最多 100
        ymax = min(100, max(25, int((max_pct + 24.99) // 25) * 25))

        x0, y0, x1, y1 = 55, 15, 745, 205  # 绘图区
        def px(i):  # 第 i 个日期的 x 坐标
            return x0 + (x1 - x0) * (i / (n - 1)) if n > 1 else (x0 + x1) / 2
        def py(v):  # 百分比 v 的 y 坐标
            return y1 - (y1 - y0) * (min(v, ymax) / ymax)

        parts = []
        # 网格线 + y 轴刻度
        steps = 4
        for s in range(steps + 1):
            v = ymax * s / steps
            y = py(v)
            parts.append(f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" stroke="#374151" stroke-width="1"/>')
            parts.append(f'<text x="{x0 - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#9ca3af" font-size="11">{v:.0f}%</text>')
        # x 轴日期标签（首/中/尾）
        for i in {0, n // 2, n - 1}:
            parts.append(f'<text x="{px(i):.1f}" y="{y1 + 18}" text-anchor="middle" fill="#9ca3af" font-size="11">{str(dates[i])[5:]}</text>')
        # 每台打印机的折线
        legends = []
        for idx, m in enumerate(monthly_usage):
            color = colors[idx % len(colors)]
            pts = " ".join(
                f"{px(dates.index(p.date)):.1f},{py(p.utilization_percent):.1f}"
                for p in sorted(m.points, key=lambda p: p.date)
            )
            if pts:
                parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>')
            legends.append(
                f'<span class="legend-item"><span class="dot" style="background:{color}"></span>{m.printer_name}</span>'
            )

        return (
            f'<svg viewBox="0 0 760 235" style="width:100%;height:auto">{"".join(parts)}</svg>'
            f'<div class="legend">{"".join(legends)}</div>'
        )

    def _build_overview_html(self, statuses: dict, snapshots: dict[str, str],
                             today_usage: list = None, monthly_usage: list = None) -> str:
        """生成自包含的监控总览 HTML，内容与网页面板四个区块一致"""
        from datetime import datetime
        from html import escape
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today_usage = today_usage or []
        monthly_usage = monthly_usage or []

        # 区块1：📷 实时监控（摄像头截图）
        cam_cards = []
        for serial, name in self._printer_names.items():
            img_b64 = snapshots.get(serial, "")
            img_html = (
                f'<img src="data:image/jpeg;base64,{img_b64}" alt="{escape(name)}">'
                if img_b64 else '<div class="no-img">无摄像头画面</div>'
            )
            loc = self._printer_locations.get(serial, "")
            st = statuses.get(serial)
            state_val = st.state.value if st else "OFFLINE"
            cam_cards.append(f"""
    <div class="card">
      <div class="card-head"><h3>{escape(name)}</h3><span class="loc">{escape(loc)}</span></div>
      <div class="cam-wrap">{img_html}<span class="state-tag">{state_val}</span></div>
    </div>""")

        # 区块2：📊 打印机状态（与网页卡片字段一致）
        stat_cards = []
        for serial, name in self._printer_names.items():
            st = statuses.get(serial)
            if st:
                state_val = st.state.value
                emoji = _STATE_EMOJI.get(state_val, "❓")
                progress = st.progress
                h, m = divmod(st.remaining_time, 60)
                eta = (f"{h}小时{m}分钟" if h else f"{m}分钟") if st.remaining_time > 0 else "--"
                rows = f"""
        <tr><td>状态</td><td>{emoji} {state_val}</td></tr>
        <tr><td>进度</td><td>{progress}%</td></tr>
        <tr><td>喷嘴温度</td><td>{st.nozzle_temp:.0f} / {st.nozzle_target_temp:.0f}℃</td></tr>
        <tr><td>热床温度</td><td>{st.bed_temp:.0f} / {st.bed_target_temp:.0f}℃</td></tr>
        <tr><td>机箱温度</td><td>{st.chamber_temp:.0f}℃</td></tr>
        <tr><td>剩余时间</td><td>{eta}</td></tr>
        <tr><td>层数</td><td>{st.current_layer} / {st.total_layers}</td></tr>
        <tr><td>文件</td><td>{escape(st.gcode_file) or '--'}</td></tr>
        <tr><td>HMS 异常</td><td>{len(st.hms_errors) or '无'}</td></tr>"""
            else:
                progress = 0
                rows = '<tr><td>状态</td><td>⚫ 无数据</td></tr>'
            stat_cards.append(f"""
    <div class="card">
      <h3>{escape(name)}</h3>
      <div class="bar"><div class="fill" style="width:{progress}%"></div></div>
      <table>{rows}</table>
    </div>""")

        # 区块3：🏭 今日产能饱和度
        util_rows = []
        for u in today_usage:
            pct = u.utilization_percent
            util_rows.append(f"""
    <div class="util-row">
      <span class="util-name">{escape(u.printer_name)}</span>
      <span class="util-pct">{pct:.1f}%</span>
      <div class="util-track"><div class="util-fill" style="width:{min(pct, 100):.1f}%"></div></div>
      <span class="util-time">{escape(u.print_time_display)}</span>
    </div>""")
        util_html = "".join(util_rows) or '<div class="no-img">暂无产能数据</div>'

        # 区块4：📈 近30天产能趋势（SVG 折线图）
        chart_html = self._build_monthly_svg(monthly_usage)

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bumbo Stats 监控总览 {now}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: #111827; color: #e5e7eb; margin: 0; padding: 24px; }}
  h1 {{ font-size: 20px; margin: 0; }} .ts {{ color: #9ca3af; font-size: 13px; margin: 6px 0 4px; }}
  h2 {{ font-size: 16px; margin: 28px 0 12px; border-left: 3px solid #10b981; padding-left: 10px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }}
  .card {{ background: #1f2937; border-radius: 12px; padding: 16px; }}
  .card h3 {{ font-size: 15px; margin: 0 0 10px; }}
  .card-head {{ display: flex; justify-content: space-between; align-items: center; }}
  .loc {{ color: #9ca3af; font-size: 12px; }}
  .cam-wrap {{ position: relative; }}
  .cam-wrap img {{ width: 100%; border-radius: 8px; display: block; }}
  .state-tag {{ position: absolute; top: 8px; left: 8px; background: rgba(0,0,0,.6);
                padding: 2px 8px; border-radius: 6px; font-size: 12px; }}
  .no-img {{ height: 180px; display: flex; align-items: center; justify-content: center;
             background: #374151; border-radius: 8px; color: #9ca3af; }}
  .bar {{ height: 6px; background: #374151; border-radius: 3px; margin: 10px 0; }}
  .fill {{ height: 100%; background: #10b981; border-radius: 3px; }}
  table {{ width: 100%; font-size: 13px; border-collapse: collapse; }}
  td {{ padding: 4px 0; border-bottom: 1px solid #374151; }}
  td:first-child {{ color: #9ca3af; width: 90px; }}
  .panel {{ background: #1f2937; border-radius: 12px; padding: 16px; }}
  .util-row {{ display: flex; align-items: center; gap: 12px; padding: 8px 0;
               border-bottom: 1px solid #374151; }}
  .util-name {{ width: 90px; font-size: 14px; }}
  .util-pct {{ width: 60px; text-align: right; font-size: 14px; color: #10b981; }}
  .util-track {{ flex: 1; height: 8px; background: #374151; border-radius: 4px; }}
  .util-fill {{ height: 100%; background: #10b981; border-radius: 4px; }}
  .util-time {{ width: 80px; text-align: right; color: #9ca3af; font-size: 13px; }}
  .legend {{ margin-top: 8px; display: flex; gap: 16px; flex-wrap: wrap; font-size: 13px; }}
  .legend-item {{ display: inline-flex; align-items: center; gap: 6px; }}
  .dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}
</style>
</head>
<body>
<h1>🖨️ Bumbo Stats 监控总览</h1>
<div class="ts">快照时间: {now}</div>
<h2>📷 实时监控</h2>
<div class="grid">{''.join(cam_cards)}
</div>
<h2>📊 打印机状态</h2>
<div class="grid">{''.join(stat_cards)}
</div>
<h2>🏭 今日产能饱和度</h2>
<div class="panel">{util_html}
</div>
<h2>📈 近30天产能饱和度趋势</h2>
<div class="panel">{chart_html}
</div>
</body>
</html>"""

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
        elif event == EVENT_PROGRESS50:
            title = "⏳ 打印进度已过半 (50%)"
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
