"""飞书智能机器人通知模块 — 在打印关键节点推送摄像头截图到群聊

飞书机器人 API:
  1. 获取 tenant_access_token: POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
  2. 发送消息: POST https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id
  3. 应用内加急（强制弹窗+响铃）: PATCH https://open.feishu.cn/open-apis/im/v1/messages/:message_id/urgent_app

入站消息接收: lark-oapi WSClient 长连接 + EventDispatcherHandler 处理 im.message.receive_v1

触发事件:
  - layer3:    打印到达第 3 层（首层确认，打印正常进行中）
  - completed: 打印完成（进度 100% / 状态 FINISH）
  - error:     HMS 健康监控错误（堵头/卡料/缺料等）
  - ai_alarm:  AI 视觉检测到打印异常

群聊指令（@机器人发含关键词的消息触发，与企业微信指令保持一致）:
  - 截图 / 监控   : 发送所有打印机实时摄像头截图
  - 状态           : 文字回复所有打印机当前状态
  - 总览           : 生成监控面板完整长截图
  - 阈值 [0~1]    : 修改 AI 异常检测阈值（不填数字则查询），会持久化到 yaml
  - 帮助           : 显示所有支持的指令

加急（urgent_app）说明:
  - 加急用户的飞书客户端会强制弹窗 + 响铃（即使开了免打扰也响）
  - 必须由被加急的用户点击才能关闭弹窗
  - 用户未读加急总数上限 200 条
  - 完全免费，不消耗任何额度

共享逻辑（指令解析 / 阈值 / 总览 HTML / 截图捕获等）从 .notify_common 复用。
"""

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from ..config import PrinterConfig
from . import notify_common as nc
from .wecom import normalize_gcode

logger = logging.getLogger(__name__)

# 静默第三方 SDK 日志（保留 lark 的 INFO 用于诊断 WSClient 连接状态）
for _lib in ["urllib3"]:
    _l = logging.getLogger(_lib)
    _l.setLevel(logging.ERROR)
    _l.disabled = True
# 强制把 lark SDK 内部 logger 调到 INFO（默认是 WARNING，会屏蔽 connected to ... 等关键日志）
try:
    from lark_oapi.core.log import logger as _lark_logger
    _lark_logger.setLevel(logging.INFO)
    _lark_logger.propagate = True
except Exception:
    pass

# 回调计数器：用于诊断 "飞书机器人未回复" 问题。
# _on_message_received 触发时自增；健康检查时报告累计值。
# 若发指令后累计值不增长，说明 SDK 未将消息路由过来（多半是飞书后台未订阅
# im.message.receive_v1 事件，或者应用没有 im:message 权限）。
_CALLBACK_INVOKED = 0

# 通知事件类型（从 notify_common 复用，确保 wecom / feishu 一致）
EVENT_LAYER3 = nc.EVENT_LAYER3
EVENT_PROGRESS50 = nc.EVENT_PROGRESS50
EVENT_COMPLETED = nc.EVENT_COMPLETED
EVENT_ERROR = nc.EVENT_ERROR
EVENT_AI_ALARM = nc.EVENT_AI_ALARM

# 每次通知发送的截图数量和间隔（降低挤出机遮挡概率）
FRAMES_PER_NOTIFICATION = nc.FRAMES_PER_NOTIFICATION
FRAME_INTERVAL = nc.FRAME_INTERVAL

# 加急限速窗口：1 小时
URGENT_WINDOW_SECONDS = 3600.0

# 长连接检查间隔（秒）：lark-oapi 的 WSClient 有 auto_reconnect，
# 但为了心跳/事件处理我们仍定期检查任务状态。
CONNECTION_CHECK_INTERVAL = 60.0


@dataclass
class _PrinterNotifState:
    """单台打印机的通知状态追踪"""
    last_gcode: str = ""
    layer3_sent: bool = False
    progress50_sent: bool = False
    completed_sent: bool = False
    last_error_snapshot: str = ""  # 上次通知的错误快照（防止同错误重复通知）

    def reset_if_new_job(self, gcode: str):
        """检测到新打印任务时重置通知状态（文件名归一化后比较）

        与 wecom._PrinterNotifState.reset_if_new_job 保持一致——同一任务
        的 gcode 可能在不同阶段被上报为 "task" 或 "task.3mf"，若不归一化
        比较，每次形式变化都会被误判为新任务，已发送的 layer3/progress50/
        completed 标记会被错误重置，进而在同一任务内多次重发通知。
        """
        if not gcode:
            return
        if normalize_gcode(gcode) != normalize_gcode(self.last_gcode):
            self.last_gcode = gcode
            self.layer3_sent = False
            self.progress50_sent = False
            self.completed_sent = False
            self.last_error_snapshot = ""
        else:
            # 同一任务但文件名形式不同（云端任务名 vs MQTT 文件名），同步为最新
            self.last_gcode = gcode


@dataclass
class FeishuConfig:
    """飞书机器人配置"""
    app_id: str = ""
    app_secret: str = ""
    chatid: str = ""            # 群聊 chat_id，留空则使用配置的 chatid
    layer3: bool = True         # 是否在第 3 层时发送
    progress50: bool = True     # 是否在进度 50% 时发送
    completed: bool = True      # 是否在打印完成时发送
    error: bool = True          # 是否在 HMS 错误时发送
    # 应用内加急（urgent_app）配置
    urgent_user_id: str = ""            # 被加急用户的 open_id，多个用英文逗号分隔
    urgent_on_events: list = field(default_factory=lambda: ["error"])
    urgent_max_per_hour: int = 10       # 单用户每小时最多加急次数，0 = 不限


class FeishuNotifier:
    """飞书机器人通知推送器

    使用方法:
        notifier = FeishuNotifier(config, printer_configs, camera_manager,
                printer_manager=manager, db=db)
        await notifier.start()         # 启动后台任务
        notifier.notify(serial, ...)   # 入队通知（非阻塞）
        await notifier.stop()          # 停止
    """

    def __init__(
        self,
        feishu_config: FeishuConfig,
        printer_configs: list[PrinterConfig],
        camera_manager=None,
        printer_manager=None,
        db=None,
        dashboard_url: str = "",
    ):
        self._cfg = feishu_config
        self._dashboard_url = dashboard_url  # 阈值响应结尾的面板链接（来自 settings.dashboard_url）
        self._printer_names = {c.serial: c.name for c in printer_configs}
        self._camera = camera_manager
        self._manager = printer_manager
        self._db = db

        # 通知队列
        self._queue: asyncio.Queue[dict] = asyncio.Queue()

        # 每台打印机的通知状态
        self._states: dict[str, _PrinterNotifState] = {
            c.serial: _PrinterNotifState() for c in printer_configs
        }

        # 飞书 API token
        self._tenant_token: Optional[str] = None
        self._token_expires_at: float = 0

        # 解析加急用户列表
        self._urgent_user_ids: list[str] = [
            uid.strip() for uid in (self._cfg.urgent_user_id or "").split(",")
            if uid.strip()
        ]
        # 加急节流：每个用户最近 1 小时加急时间戳
        self._urgent_history: dict[str, deque] = {
            uid: deque() for uid in self._urgent_user_ids
        }

        # AI Guard 引用（用于支持阈值指令）
        self._ai_guard = None

        # 群聊指令冷却控制（委托给 notify_common.CommandCooldown）
        self._cmd_cooldown = nc.CommandCooldown()

        # 飞书 WebSocket 客户端（由 start() 创建）
        self._ws_client = None
        self._ws_thread = None  # 在独立线程中运行 SDK 自带的事件循环

        # 后台任务
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def bind_ai_guard(self, ai_guard):
        """注入 AIGuardLoop 引用，用于响应 /threshold 命令"""
        self._ai_guard = ai_guard

    def update_config(self, new_cfg: FeishuConfig):
        """运行时更新配置（来自 /api/settings/feishu 重载）"""
        self._cfg = new_cfg
        self._urgent_user_ids = [
            uid.strip() for uid in (new_cfg.urgent_user_id or "").split(",")
            if uid.strip()
        ]
        # 重新初始化节流桶（保留原有记录）
        for uid in self._urgent_user_ids:
            if uid not in self._urgent_history:
                self._urgent_history[uid] = deque()
        # 移除已删除用户的桶
        for uid in list(self._urgent_history.keys()):
            if uid not in self._urgent_user_ids:
                del self._urgent_history[uid]
        logger.info(
            "飞书配置已重载: layer3=%s progress50=%s completed=%s error=%s urgent_users=%s urgent_events=%s",
            new_cfg.layer3, new_cfg.progress50, new_cfg.completed, new_cfg.error,
            self._urgent_user_ids, new_cfg.urgent_on_events,
        )

    def notify(
        self,
        serial: str,
        event: str,
        status=None,
        errors: list = None,
        ai_alert=None,
        alarm_frames: list = None,
    ):
        """将通知事件加入队列（线程安全，非阻塞）

        Args:
            serial: 打印机序列号
            event: 事件类型 (EVENT_LAYER3 / EVENT_PROGRESS50 / EVENT_COMPLETED / EVENT_ERROR / EVENT_AI_ALARM)
            status: 当前 PrinterStatus（可选，用于丰富消息内容）
            errors: 错误列表（仅 EVENT_ERROR 时使用）
            ai_alert: DetectionResult（仅 EVENT_AI_ALARM 时使用）
            alarm_frames: 触发报警的连续帧列表（仅 EVENT_AI_ALARM 时使用）
        """
        state = self._states.get(serial)
        if not state:
            return

        # 去重检查
        gcode = getattr(status, "gcode_file", "") or ""
        state.reset_if_new_job(gcode)

        # AI 报警事件：复用 error 开关；冷却由 AIGuardLoop 控制
        if event == EVENT_AI_ALARM:
            if not self._cfg.error:
                return
        elif event == EVENT_LAYER3:
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
                snapshot = json.dumps(
                    sorted((e.code, e.severity, e.message) for e in errors),
                )
                if state.last_error_snapshot == snapshot:
                    return
                state.last_error_snapshot = snapshot
        else:
            logger.warning("飞书未支持的事件类型: %s", event)
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
            log_extra = (
                f" P={ai_alert.probability:.2f}" if event == EVENT_AI_ALARM and ai_alert else ""
            )
            logger.info("飞书通知入队: %s [%s]%s", printer_name, event, log_extra)
        except asyncio.QueueFull:
            logger.warning("通知队列已满，丢弃: %s [%s]", printer_name, event)

    async def start(self):
        """启动后台飞书长连接与通知处理任务"""
        if not self._cfg.app_id or not self._cfg.app_secret:
            logger.warning("飞书 app_id/app_secret 未配置，跳过通知模块")
            return
        if self._running:
            logger.warning("飞书通知模块已在运行")
            return
        self._running = True

        # 创建并启动 WebSocket 长连接（用于接收群聊指令）
        # lark-oapi 1.5 的 WSClient.start() 在模块顶部执行了
        #   loop = asyncio.get_event_loop()  （或 new_event_loop+set_event_loop）
        # 该 loop 变量在 import 时绑定到主线程（uvicorn）的事件循环对象，
        # start() 内部用此 loop.run_until_complete(self._connect())，在主线程
        # 直接调用会与 uvicorn 冲突，在 daemon 线程调用则会触发
        # "This event loop is already running"。
        #
        # 解决方案: 在独立 daemon 线程里运行 start()，该线程创建专属事件循环，
        # 并 monkey-patch lark_oapi.ws.client.loop 指向新循环。同时重建
        # Client._lock 让其绑定到新循环（避免 "lock is bound to a different loop"）。
        try:
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
            from lark_oapi.ws.client import Client as LARKClient
            from lark_oapi.core.enum import LogLevel as LARKLogLevel
            import threading
            import asyncio as _aio

            handler = (
                EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(self._on_message_received)
                .build()
            )
            self._ws_client = LARKClient(
                app_id=self._cfg.app_id,
                app_secret=self._cfg.app_secret,
                log_level=LARKLogLevel.INFO,
                event_handler=handler,
                auto_reconnect=True,
            )

            def _run_ws_client():
                """在线程内运行 SDK 的 start()，并把模块级 loop 切换为本线程的新循环"""
                import lark_oapi.ws.client as _lc
                new_loop = _aio.new_event_loop()
                _aio.set_event_loop(new_loop)
                _lc.loop = new_loop  # 关键: monkey-patch SDK 模块级 loop
                # 重建 _lock 绑定到新循环
                if self._ws_client is not None:
                    try:
                        self._ws_client._lock = _aio.Lock()
                    except Exception as e:
                        logger.debug("重建 _lock 失败（可能不影响运行）: %s", e)
                try:
                    logger.info("飞书 WSClient 即将启动 start() (thread=%s)", threading.current_thread().name)
                    self._ws_client.start()
                    logger.warning("飞书 WSClient.start() 已返回（异常结束）")
                except Exception as e:
                    import traceback
                    logger.error(
                        "飞书 WSClient 异常退出: %s\n%s",
                        e, traceback.format_exc(),
                    )
                finally:
                    try:
                        new_loop.close()
                    except Exception:
                        pass

            self._ws_thread = threading.Thread(
                target=_run_ws_client,
                name="feishu-ws-client",
                daemon=True,
            )
            self._ws_thread.start()
            logger.info("飞书长连接已在后台线程启动（接收群聊指令）")
        except Exception as e:
            import traceback
            logger.error(
                "飞书长连接启动失败（出站通知仍可用）: %s\n%s",
                e, traceback.format_exc(),
            )
            self._ws_client = None
            self._ws_thread = None

        self._task = asyncio.create_task(self._run_loop())
        self._health_task = asyncio.create_task(self._ws_health_loop())
        logger.info(
            "飞书通知模块已启动 (urgent_users=%s, urgent_events=%s)",
            self._urgent_user_ids, self._cfg.urgent_on_events,
        )

    async def stop(self):
        """停止后台任务并断开连接"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if getattr(self, "_health_task", None):
            self._health_task.cancel()
            try:
                await self._health_task
            except (asyncio.CancelledError, Exception):
                pass
        # 长连接在独立 daemon 线程中运行，进程退出时自动结束
        logger.info("飞书通知模块已停止")

    async def _ws_health_loop(self):
        """定期检查 WSClient 长连接健康状态并打印诊断日志。

        SDK 启动后只在连接成功/断开时才会打日志，平时静默。
        我们每 30s 主动检查 self._ws_client._conn 是否存在，
        以及后台线程是否还活着（daemon 线程一旦 start() 异常退出就废了）。
        """
        import threading as _th
        while self._running:
            try:
                await asyncio.sleep(30.0)
            except asyncio.CancelledError:
                break
            try:
                if self._ws_client is None:
                    logger.warning("[健康检查] WSClient 为 None，长连接未启动")
                    continue
                conn = getattr(self._ws_client, "_conn", None)
                alive = (
                    self._ws_thread is not None
                    and self._ws_thread.is_alive()
                )
                logger.info(
                    "[健康检查] WSClient 连接对象=%s, 后台线程存活=%s, "
                    "回调累计=%d, app_id=%s..., chatid=%s",
                    "✅ 已连接" if conn is not None else "❌ 未连接",
                    alive,
                    _CALLBACK_INVOKED,
                    (self._cfg.app_id or "")[:12],
                    self._cfg.chatid[:16] if self._cfg.chatid else "(空)",
                )
                if not alive and self._running:
                    logger.error(
                        "[健康检查] WSClient 后台线程已退出，长连接失效！"
                        "重启服务可恢复"
                    )
            except Exception as e:
                logger.debug("[健康检查] 异常: %s", e)

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
            # frames 是 list[(name, bytes)]，发给 _send_message 时只传 bytes
            # （aiohttp.FormData 会把 tuple 当成 iterable of payload，报序列化错误）
            image_frames = [f for _, f in frames]

            content = f"""🧪 配置测试消息

{now}

发送者: Bumbo Stats 监控面板

测试打印机截图:
{printer_list}

如果看到此消息，说明飞书配置成功！"""

            # 发送消息（只传 bytes 列表；post 富文本一条消息，title 作为标题）
            message_id = await self._send_message(
                content, image_frames, title="🧪 配置测试消息",
            )
            logger.info("飞书测试消息发送完成 (message_id=%s)", message_id)
            return message_id is not None

        except Exception as e:
            logger.error("发送飞书测试消息失败: %s", e)
            return False

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _run_loop(self):
        """后台主循环：获取 token + 处理通知队列 + 处理指令队列"""
        from datetime import datetime
        while self._running:
            try:
                # 获取 tenant_access_token
                await self._refresh_token()

                # 处理队列
                while self._running:
                    try:
                        item = await asyncio.wait_for(
                            self._queue.get(), timeout=5.0
                        )
                        event = item.get("event", "")
                        # 指令事件 vs 通知事件分发
                        if event in ("snapshot", "status", "overview", "help", "threshold"):
                            await self._process_command(item)
                        else:
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

    # ------------------------------------------------------------------
    # 入站消息处理（lark-oapi 事件回调）
    # ------------------------------------------------------------------

    def _on_message_received(self, data):
        """飞书 lark-oapi 入站消息回调（同步函数，内部 submit 到队列）

        群消息（chat_type in {group, super_group}）中 @机器人 发含关键词的消息
        会触发对应指令。机器人单聊消息不在此响应（避免打扰个人聊天）。

        此函数是 lark-oapi 的同步回调，由 SDK 在事件循环中调用，
        我们把解析后的指令 put 到 self._queue 里异步处理。
        """
        # 诊断：不论后续是否被过滤，进入回调时都打一条 INFO 日志
        # —— 这样若 "指令无回复" 能马上区分：SDK 收没收到 / 回调有没有被调
        global _CALLBACK_INVOKED
        _CALLBACK_INVOKED += 1
        try:
            ev = getattr(data, "event", None) or data
            msg = getattr(ev, "message", None) if ev is not None else None
            chat_type_dbg = getattr(msg, "chat_type", "") if msg else ""
            message_type_dbg = getattr(msg, "message_type", "") if msg else ""
            sender = getattr(ev, "sender", None)
            sender_type_dbg = getattr(sender, "sender_type", "") if sender else ""
            logger.info(
                "飞书 SDK 回调触发 #%d: chat_type=%s message_type=%s sender_type=%s",
                _CALLBACK_INVOKED, chat_type_dbg, message_type_dbg, sender_type_dbg,
            )
        except Exception as e:
            logger.warning("飞书回调诊断日志输出失败（不影响处理）: %s", e)

        try:
            # lark-oapi 1.5+ 数据结构：data.event 为 P2ImMessageReceiveV1 内部结构
            ev = getattr(data, "event", None)
            if ev is None:
                # 也可能在 header 下；兼容老版本
                ev = data
            msg = getattr(ev, "message", None)
            if msg is None:
                logger.debug("飞书事件不含 message，跳过")
                return

            chat_type = getattr(msg, "chat_type", "") or ""
            message_type = getattr(msg, "message_type", "") or ""

            # 仅响应群聊 + 文本消息
            if chat_type not in ("group", "super_group", "thread"):
                logger.info("飞书入站消息被忽略: chat_type=%s（非群聊）", chat_type)
                return
            if message_type != "text":
                logger.info("飞书入站消息被忽略: message_type=%s（非文本）", message_type)
                return

            # 提取文本（content 是 JSON: {"text": "@_user_1 截图"}）
            content = getattr(msg, "content", "") or ""
            try:
                content_obj = json.loads(content) if content else {}
            except (json.JSONDecodeError, TypeError):
                content_obj = {}
            text = (content_obj.get("text") or "").strip()
            logger.info(
                "飞书入站消息: chat=%s msg_id=%s text=%r",
                getattr(msg, "chat_id", "")[:12],
                getattr(msg, "message_id", "")[:14],
                text[:80],
            )

            # 必须 @机器人 才视为指令（避免响应群里其他人的同名消息）
            sender = getattr(ev, "sender", None)
            mentions = getattr(msg, "mentions", None) or []
            bot_mentioned = False
            if sender and getattr(sender, "sender_type", "") == "app":
                bot_mentioned = True  # 自身发的
            if mentions and not bot_mentioned:
                for m in mentions:
                    key = getattr(m, "key", "")
                    # mentions 里的 key 形如 "@_user_1"，需用 in 而不是 ==
                    # （原文可能是 "@_user_1 截图"）
                    if key and key in text:
                        bot_mentioned = True
                        text = text.replace(key, "").strip()
                        break
            if not bot_mentioned:
                logger.info("飞书入站消息未 @机器人，忽略: text=%r", text[:60])
                return
            if not text:
                logger.info("飞书入站消息 @机器人 但无文本内容，忽略")
                return

            # 解析指令
            parsed = nc.parse_command(text)
            if not parsed:
                logger.info("飞书入站消息 @机器人 但不匹配任何指令: text=%r", text[:60])
                return
            if not self._cmd_cooldown.check(parsed.command):
                logger.info("飞书入站消息 [%s] 在冷却中，忽略: text=%r", parsed.command, text[:60])
                return

            try:
                self._queue.put_nowait({
                    "event": parsed.command, "ts": time.time(),
                    "raw_text": parsed.raw_text,
                    "numeric_value": parsed.numeric_value,
                    "chat_id": getattr(msg, "chat_id", ""),
                    "message_id": getattr(msg, "message_id", ""),
                })
                logger.info(
                    "✅ 收到飞书群指令 [%s]: %s（已入队）",
                    parsed.command, parsed.raw_text[:60],
                )
            except asyncio.QueueFull:
                logger.warning("通知队列已满，丢弃指令 [%s]", parsed.command)

        except Exception as e:
            import traceback
            logger.error(
                "处理飞书入站消息异常: %s\n%s", e, traceback.format_exc(),
            )

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
        """处理一条通知：抓帧 → 上传 → 发送 → 加急"""
        serial = item["serial"]
        printer_name = item["printer_name"]
        event = item["event"]
        status = item.get("status")
        errors = item.get("errors")
        ai_alert = item.get("ai_alert")
        alarm_frames = item.get("alarm_frames")

        # 确保有有效的 token
        if not self._tenant_token or time.time() >= self._token_expires_at:
            await self._refresh_token()
            if not self._tenant_token:
                logger.warning("飞书 token 不可用，跳过通知")
                return

        # 截图获取：AI 报警帧优先；HMS 错误不发图；其他事件实时抓 1 张
        if alarm_frames:
            frames = alarm_frames[:1]
        elif event == EVENT_ERROR:
            frames = []
        else:
            all_frames = await self._get_camera_frames(serial)
            frames = all_frames[:1] if all_frames else []

        # 构建消息
        content = self._build_message(
            printer_name, event, status, errors, ai_alert=ai_alert,
        )

        # 发送消息 + 加急（post 富文本一条消息；content head 行已含
        # [机器名]，所以不传 title，避免渲染出多余的标题行）
        try:
            message_id = await self._send_message(content, frames, title="")
            if message_id:
                logger.info("✅ 飞书通知已发送: %s [%s] msg=%s",
                            printer_name, event, message_id)
                # 应用内加急（如果配置了该事件且未超过限速）
                if self._should_urgent(event):
                    await self._urgent_message(message_id, printer_name, event)
            else:
                logger.warning("飞书通知未取到 message_id: %s [%s]",
                               printer_name, event)
        except Exception as e:
            logger.error("飞书发送失败: %s [%s] %s", printer_name, event, e)

    def _should_urgent(self, event: str) -> bool:
        """判断该事件是否应触发应用内加急"""
        if not self._urgent_user_ids:
            return False
        if event not in self._cfg.urgent_on_events:
            return False
        # 限速检查（按用户）
        if self._cfg.urgent_max_per_hour <= 0:
            return True
        now = time.time()
        for uid in self._urgent_user_ids:
            hist = self._urgent_history.get(uid)
            if not hist:
                continue
            # 清理 1 小时外的记录
            while hist and now - hist[0] > URGENT_WINDOW_SECONDS:
                hist.popleft()
            if len(hist) >= self._cfg.urgent_max_per_hour:
                logger.debug("飞书加急限速: 用户 %s 1 小时已加急 %d 次",
                             uid, len(hist))
                return False
        return True

    def _record_urgent(self):
        """记录本次加急（节流计数）"""
        now = time.time()
        for uid in self._urgent_user_ids:
            hist = self._urgent_history.setdefault(uid, deque())
            while hist and now - hist[0] > URGENT_WINDOW_SECONDS:
                hist.popleft()
            hist.append(now)

    async def _urgent_message(self, message_id: str, printer_name: str, event: str):
        """调用飞书应用内加急 API（让用户客户端强制弹窗 + 响铃）

        API: PATCH https://open.feishu.cn/open-apis/im/v1/messages/:message_id/urgent_app
        """
        import aiohttp

        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/urgent_app?user_id_type=open_id"
        headers = {
            "Authorization": f"Bearer {self._tenant_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        payload = {"user_id_list": self._urgent_user_ids}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.patch(url, headers=headers, json=payload, timeout=10) as resp:
                    data = await resp.json()
                    if data.get("code") == 0:
                        self._record_urgent()
                        logger.info("🔔 飞书应用内加急已触发: %s [%s] users=%s",
                                    printer_name, event, self._urgent_user_ids)
                        invalid = (data.get("data") or {}).get("invalid_user_id_list") or []
                        if invalid:
                            logger.warning("以下用户不在群内，加急无效: %s", invalid)
                    else:
                        err_code = data.get("code")
                        err_msg = data.get("msg")
                        logger.error("飞书应用内加急失败: code=%s msg=%s (msg=%s)",
                                     err_code, err_msg, message_id)
        except Exception as e:
            logger.warning("飞书应用内加急异常: %s", e)

    async def _send_message(
        self,
        content: str,
        frames: list,
        title: str = "",
    ) -> Optional[str]:
        """发送飞书富文本消息（文字 + 多张图片合并为一条 post 消息）

        飞书 post 消息格式:
          content.post.zh_cn.title   标题（可选）
          content.post.zh_cn.content 二维数组，每个内层数组是一段
                                    段内可以混合 text / img / at 等 tag

        改造前: 先发一条文字消息、再逐张发图片消息，群里是 N+1 条独立气泡
        改造后: 一条 post 富文本，文字 + N 张图片都在同一条消息里

        Args:
            content: 文字内容（用 \\n 分行，每行是一段）
            frames: 图片帧 bytes 列表
            title: 消息标题（可选，打印机名称 / 测试标识 / 事件名等）

        Returns:
            post 消息的 message_id（用于后续应用内加急）；发送失败返回 None
        """
        chat_id = self._cfg.chatid
        if not chat_id:
            logger.warning("飞书 chatid 未配置，跳过发送")
            return None
        if not self._tenant_token:
            logger.warning("飞书 tenant_token 不可用，跳过发送")
            return None

        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {
            "Authorization": f"Bearer {self._tenant_token}",
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            # 1. 上传所有图片获取 image_key（复用同一 session）
            image_keys: list[str] = []
            for frame in frames:
                if not isinstance(frame, (bytes, bytearray)):
                    logger.warning("飞书图片帧类型错误: %s", type(frame))
                    continue
                key = await self._upload_image(session, bytes(frame))
                if key:
                    image_keys.append(key)

            # 2. 构建 post 富文本内容（二维数组）
            content_blocks: list[list[dict]] = []
            # 文字按行拆段（保留空行作为段落间隔）
            for line in (content or "").split("\n"):
                content_blocks.append([{"tag": "text", "text": line}])
            # 图片追加到末尾（每张图独立一段，渲染为缩略图列表）
            for key in image_keys:
                content_blocks.append([{"tag": "img", "image_key": key}])
            if not content_blocks:
                content_blocks.append([{"tag": "text", "text": "(无内容)"}])

            # 3. 一次性发送 post 富文本消息
            zh_cn: dict = {"content": content_blocks}
            # 飞书 post 标题为空时不再传 title 字段，避免渲染出多余的标题行
            # （自动通知正文的 head 行已含 [机器名]，不需要额外的标题）
            if title:
                zh_cn["title"] = title
            post = {"zh_cn": zh_cn}
            payload = {
                "receive_id": chat_id,
                "msg_type": "post",
                "content": json.dumps({"post": post}),
            }
            try:
                async with session.post(url, headers=headers, json=payload, timeout=15) as resp:
                    data = await resp.json()
                    if data.get("code") != 0:
                        logger.error("飞书富文本消息发送失败: %s", data.get("msg"))
                        return None
                    message_id = (data.get("data") or {}).get("message_id")
                    text_n = sum(
                        1 for b in content_blocks
                        if b and b[0].get("tag") == "text"
                        and b[0].get("text") != "(无内容)"
                    )
                    img_n = len(image_keys)
                    logger.info(
                        "飞书富文本消息已发送: title=%r text=%d段 image=%d张 msg=%s",
                        title, text_n, img_n, message_id,
                    )
                    return message_id
            except Exception as e:
                logger.error("飞书富文本消息发送异常: %s", e)
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
    def _build_message(
        printer_name: str, event: str, status, errors=None, ai_alert=None,
    ) -> str:
        """构建通知消息（post 富文本）

        飞书 post 富文本的 text tag 不渲染 Markdown，``**xxx**`` 会原样显示星号，
        因此正文统一使用纯文本 + emoji，不再使用加粗语法。

        排版策略:
            第一行 = emoji + 机器名称 + 信息关键内容（一眼看懂事件核心）
            后续行 = 关键补充信息（紧凑、单行展示），仅在有数据时输出
        """
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        gcode = getattr(status, "gcode_file", "") or ""
        layer = getattr(status, "current_layer", 0)
        total = getattr(status, "total_layers", 0)
        progress = getattr(status, "progress", 0)
        st = getattr(status, "state", "") or ""

        # ---- 第一行: emoji + 机器名称 + 信息关键内容 ----
        if event == EVENT_LAYER3:
            head = f"🖨️ [{printer_name}] 打印已到达第 3 层"
        elif event == EVENT_PROGRESS50:
            head = f"⏳ [{printer_name}] 打印进度已过半"
        elif event == EVENT_COMPLETED:
            head = f"✅ [{printer_name}] 打印已完成"
        elif event == EVENT_ERROR:
            head = f"🚨 [{printer_name}] 打印机异常"
        elif event == EVENT_AI_ALARM:
            prob = f"{ai_alert.probability:.1%}" if ai_alert else "—"
            head = f"🤖 [{printer_name}] AI 检测到打印异常 (P={prob})"
        else:
            head = f"📢 [{printer_name}] {event}"

        lines: list[str] = [head]

        # ---- 简明补充信息: 仅输出有数据的字段 ----
        extras: list[str] = []
        if event == EVENT_LAYER3 and total > 0:
            extras.append(f"进度 {progress}% (层 {layer}/{total})")
        elif event == EVENT_PROGRESS50 and total > 0:
            extras.append(f"进度 {progress}% (层 {layer}/{total})")
        elif event == EVENT_COMPLETED and total > 0:
            extras.append(f"层数 {layer}/{total}")
        elif event == EVENT_AI_ALARM and ai_alert and ai_alert.message:
            extras.append(ai_alert.message)
        if st:
            emoji = nc.state_emoji(st)
            extras.append(f"状态 {emoji} {st}" if emoji else f"状态 {st}")
        if gcode:
            extras.append(f"文件 {gcode}")

        if extras:
            lines.append("｜".join(extras))
        lines.append(now)

        # ---- 错误详情（HMS）: 中文名 + 类目 + 建议 + 原始 + 8 位码 + 短码 + 级别 ----
        # Bambu 实际数据:
        #   err.code    = attr (4 byte, 用于查 HMS_ERROR_CODES 注册表)
        #   err.attr    = attr 原始字段 (冗余存一份便于渲染)
        #   err.raw_code= code 原始字段 (高 16 alert level + 低 16 error id)
        #   err.severity= "fatal" / "serious" / "common" / "error" / "warning"
        if event == EVENT_ERROR and errors:
            lines.append("")
            from ..models import lookup_hms_error
            severity_emoji = {
                "fatal": "🔴", "serious": "🟡", "common": "🔵",
                "error": "🔴", "warning": "🟡",
            }
            for err in errors:
                info = lookup_hms_error(err.code)
                cat = info.get("category", "未知")
                suggestion = info.get("suggestion", "")
                name = (
                    info.get("cn", err.message)
                    if err.message else info.get("cn", "未知错误")
                )
                sev = (err.severity or "").strip().lower()
                sev_tag = f"{severity_emoji.get(sev, '⚪')}{sev or '未知'}"
                # 短码: attr 高 16 + raw_code 低 16, 形如 0300_0200
                short_code = f"{(err.attr >> 16) & 0xFFFF:04X}_{err.raw_code & 0xFFFF:04X}"
                lines.append(f"• {name} ({cat})")
                if suggestion:
                    lines.append(f"  建议: {suggestion}")
                lines.append(
                    f"  原始: {err.message or '(无)'} | 码: 0x{err.code:08X}"
                    f" | 短码: {short_code} | 级别: {sev_tag}"
                )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 群聊指令处理（与 wecom 五个指令一致）
    # ------------------------------------------------------------------

    async def _process_command(self, item: dict):
        """分发群聊指令到具体处理函数"""
        chat_id = item.get("chat_id") or self._cfg.chatid
        if not chat_id:
            logger.warning("飞书 chat_id 未就绪，跳过指令: %s", item.get("event"))
            return

        cmd = item.get("event", "")
        try:
            if cmd == "snapshot":
                await self._cmd_snapshot(chat_id)
            elif cmd == "status":
                await self._cmd_status(chat_id)
            elif cmd == "overview":
                await self._cmd_overview(chat_id)
            elif cmd == "threshold":
                await self._cmd_threshold(chat_id, item.get("numeric_value"))
            elif cmd == "help":
                await self._cmd_help(chat_id)
        except Exception as e:
            logger.error("飞书指令 [%s] 处理失败: %s", cmd, e)

    async def _cmd_snapshot(self, chat_id: str):
        """@机器人 截图 / 监控  : 发送所有打印机实时摄像头截图（一条 post 富文本）"""
        if not self._tenant_token or time.time() >= self._token_expires_at:
            await self._refresh_token()
            if not self._tenant_token:
                return

        frames: list[bytes] = []
        names: list[str] = []
        missing: list[str] = []
        for serial, name in self._printer_names.items():
            frame = await nc.capture_camera_one(self._camera, serial)
            if frame:
                frames.append(frame)
                names.append(f"✅ {name}")
            else:
                missing.append(name)

        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = ["📷 实时监控截图", f"> {now}", ""]
        lines.extend(f"- {n}" for n in names)
        for name in missing:
            lines.append(f"- ❌ {name} 获取失败")
        if missing:
            lines.append("")
            lines.append("截图获取失败原因: 摄像头连接不上 / 打印机离线")

        if frames:
            message_id = await self._send_message("\n".join(lines), frames, title="📷 实时监控截图")
            if message_id:
                logger.info("✅ 飞书截图指令已响应: 成功 %d, 失败 %d",
                            len(frames), len(missing))
        else:
            # 没有可用帧，仅发文字
            message_id = await self._send_message(
                "\n".join(lines + ["", "⚠️ 所有摄像头均无画面"]),
                [], title="📷 实时监控截图",
            )
            logger.warning("飞书截图指令无可用帧: %s", len(missing))

    async def _cmd_status(self, chat_id: str):
        """@机器人 状态  : 文字回复所有打印机当前状态"""
        if not self._tenant_token or time.time() >= self._token_expires_at:
            await self._refresh_token()
            if not self._tenant_token:
                return

        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = ["📊 打印机实时状态", f"> {now}", ""]

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

        message_id = await self._send_message(
            "\n".join(lines), [], title="📊 打印机实时状态",
        )
        if message_id:
            logger.info("✅ 飞书状态指令已响应")

    async def _cmd_overview(self, chat_id: str):
        """@机器人 总览  : 生成监控面板完整长截图（与网页四个区块一致）"""
        if not self._tenant_token or time.time() >= self._token_expires_at:
            await self._refresh_token()
            if not self._tenant_token:
                return

        from datetime import datetime

        # 收集截图 / 状态 / 产能
        snapshots: dict[str, bytes] = {}
        for serial in self._printer_names:
            frame = await nc.capture_camera_one(self._camera, serial)
            if frame:
                snapshots[serial] = frame

        statuses = self._manager.get_all_statuses() if self._manager else {}

        today_usage, monthly_usage = [], []
        if self._db and self._manager:
            try:
                today_usage = await self._db.get_today_usage_all(self._manager.printer_names)
                monthly_usage = await self._db.get_monthly_usage_all(
                    self._manager.printer_names, days=30
                )
            except Exception as e:
                logger.warning("获取产能数据失败: %s", e)

        # 使用打印机 name + location（从 self._printer_names 取 name，
        # location 暂为空串——概览页面只展示 name 已足够）
        html = nc.build_overview_html(
            self._printer_names,
            {s: "" for s in self._printer_names},
            statuses, snapshots, today_usage, monthly_usage,
        )

        # 优先渲染为长截图发送
        image = await nc.render_overview_image(html)
        if image:
            message_id = await self._send_message(
                f"快照时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                [image], title="🖨️ 监控总览",
            )
            if message_id:
                logger.info("✅ 飞书总览指令已响应(长截图 %dKB)", len(image) // 1024)
            return

        # 回退：发送 HTML 文件
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            upload_result = await self._upload_file(html.encode("utf-8"), "monitor.html")
            if upload_result:
                message_id = await self._send_message(
                    f"快照时间: {ts}\n渲染失败，发送 HTML 文件备用",
                    [], title="🖨️ 监控总览",
                )
                logger.info("✅ 飞书总览指令已响应(HTML 文件)")
        except Exception as e:
            logger.error("飞书总览指令发送失败: %s", e)

    async def _cmd_threshold(self, chat_id: str, numeric_value: Optional[float]):
        """@机器人 阈值 [0~1]  : 修改或查询 AI 异常检测阈值（持久化到 yaml）"""
        if not self._tenant_token or time.time() >= self._token_expires_at:
            await self._refresh_token()
            if not self._tenant_token:
                return

        text = nc.build_threshold_message(
            self._ai_guard, numeric_value, dashboard_url=self._dashboard_url,
        )
        message_id = await self._send_message(text, [], title="🎚️ AI 检测阈值")
        if message_id:
            logger.info("✅ 飞书阈值指令已响应 (new_value=%s)", numeric_value)

    async def _cmd_help(self, chat_id: str):
        """@机器人 帮助  : 显示所有支持的指令"""
        if not self._tenant_token or time.time() >= self._token_expires_at:
            await self._refresh_token()
            if not self._tenant_token:
                return

        message_id = await self._send_message(nc.HELP_TEXT, [], title="🤖 支持的指令")
        if message_id:
            logger.info("✅ 飞书帮助指令已响应")

    async def _upload_image(self, session, frame: bytes) -> Optional[str]:
        """上传单张图片到飞书，返回 image_key（用于 post 富文本消息引用）

        API: POST https://open.feishu.cn/open-apis/im/v1/images
        字段: image_type=message, image=<JPEG bytes>
        返回: {"code":0, "data":{"image_key":"img_xxx"}}
        """
        url = "https://open.feishu.cn/open-apis/im/v1/images"
        form = aiohttp.FormData()
        form.add_field("image_type", "message")
        form.add_field("image", frame, filename="snapshot.jpg")
        headers = {"Authorization": f"Bearer {self._tenant_token}"}
        try:
            async with session.post(url, headers=headers, data=form, timeout=30) as resp:
                data = await resp.json()
                if data.get("code") == 0:
                    return (data.get("data") or {}).get("image_key")
                logger.warning("飞书图片上传失败: %s", data.get("msg"))
        except Exception as e:
            logger.warning("飞书图片上传异常: %s", e)
        return None

    async def _upload_file(self, content: bytes, filename: str) -> Optional[str]:
        """上传文件到飞书（用于总览 HTML 备用文件）

        Returns:
            file_key
        """
        url = "https://open.feishu.cn/open-apis/im/v1/files"
        form = aiohttp.FormData()
        form.add_field("file_type", "stream")
        form.add_field("file_name", filename)
        form.add_field("file", content, filename=filename)

        headers = {"Authorization": f"Bearer {self._tenant_token}"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, data=form, timeout=30) as resp:
                    data = await resp.json()
                    if data.get("code") == 0:
                        return (data.get("data") or {}).get("file_key")
                    logger.warning("飞书文件上传失败: %s", data.get("msg"))
        except Exception as e:
            logger.warning("飞书文件上传异常: %s", e)
        return None