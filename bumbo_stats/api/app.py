"""FastAPI 应用创建与生命周期管理"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..models import PrinterState
from ..auth import TokenManager
from ..config import AppConfig, PrinterConfig, load_config
from ..database import Database
from ..printer.camera import CameraManager
from ..printer.manager import PrinterManager
from ..printer.monitor import PrintMonitor
try:
    # 通知能力可选：未安装 extras 时降级为不启用（pip install "bumbo_stats[notify]"）
    from ..printer.wecom import WeComNotifier, WeComConfig
    from ..printer.feishu import FeishuNotifier, FeishuConfig
    _NOTIFY_AVAILABLE = True
except ImportError as _e:  # noqa: B902
    WeComNotifier = WeComConfig = FeishuNotifier = FeishuConfig = None  # type: ignore[assignment]
    _NOTIFY_AVAILABLE = False
    logging.getLogger(__name__).warning(
        "通知模块不可用（缺少依赖: %s），监控面板正常运行；"
        "如需企微/飞书通知请安装: pip install \"bumbo_stats[notify]\"", _e,
    )
from ..printer.ai_guard import AIGuardLoop
from .routes import router
from .websocket import ConnectionManager

logger = logging.getLogger(__name__)

# 全局引用，用于 lifespan 中取消任务
_monitor_task: asyncio.Task | None = None
_camera_task: asyncio.Task | None = None
_reconnect_task: asyncio.Task | None = None
_ai_guard: AIGuardLoop | None = None
# 字符串注解: 通知依赖缺失时两个类被置为 None，避免运行时求值 `None | None` 报 TypeError
_wecom_notifier: "WeComNotifier | None" = None
_feishu_notifier: "FeishuNotifier | None" = None
_ws_manager: ConnectionManager = ConnectionManager()

# 运行时重载函数（在 create_app 中注册到 app.state）
_reload_printers_fn = None
_reload_wecom_fn = None
_reload_feishu_fn = None


async def _broadcast(message: dict):
    """广播回调，转发到 WebSocket 连接管理器"""
    await _ws_manager.broadcast(message)


async def _reconnect_loop(manager: PrinterManager, interval: float = 60.0):
    """定期检查并重连断开的 MQTT 客户端"""
    while True:
        try:
            await asyncio.sleep(interval)
            await manager.reconnect_all()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("重连检查异常: %s", e)


async def _reload_printers_impl(new_printers: list[PrinterConfig]):
    """运行时重载打印机配置"""
    global _reload_printers_fn
    manager = None
    camera = None
    for attr in ["_manager", "_camera"]:
        if hasattr(router, "_app") and hasattr(router._app.state, attr):
            obj = getattr(router._app.state, attr)
            if attr == "_manager":
                manager = obj
            elif attr == "_camera":
                camera = obj
    if not manager:
        logger.warning("无法获取 manager 实例")
        return

    old_serials = set(manager.serials)
    new_serials = {p.serial for p in new_printers}

    # 移除不存在的打印机（add_printer/remove_printer 是 async，必须 await，
    # 否则只是创建了协程对象而从未执行，_configs 不会更新，
    # 导致新打印机的状态/产能/趋势接口全部拿不到数据）
    for serial in old_serials - new_serials:
        await manager.remove_printer(serial)
        logger.info("已移除打印机: %s", serial)

    # 添加新打印机
    for printer in new_printers:
        if printer.serial in new_serials - old_serials:
            await manager.add_printer(printer)
            logger.info("已添加打印机: %s (%s)", printer.name, printer.serial)

    # 重载摄像头配置（即时更新 access_code 等）
    if camera:
        camera.reload_configs(new_printers)


async def _reload_wecom_impl(new_wecom: WeComConfig):
    """运行时重载企业微信配置

    使用 WeComNotifier.update_config 更新内存中的配置；
    当 enabled 状态发生变化时，同步启停后台任务：
    - 关闭：停止 WeComNotifier（释放 WebSocket 连接）
    - 开启：启动 WeComNotifier（重建连接，接收群聊指令）
    """
    wecom = None
    for attr in ["_wecom_notifier"]:
        if hasattr(router, "_app") and hasattr(router._app.state, attr):
            wecom = getattr(router._app.state, attr)
            break
    if not wecom:
        logger.warning("无法获取 wecom_notifier 实例")
        return

    # update_config 返回 enabled 是否发生变化
    changed = wecom.update_config(new_wecom)
    if changed:
        if new_wecom.enabled:
            logger.info("企业微信总开关打开，启动后台任务")
            await wecom.start()
        else:
            logger.info("企业微信总开关关闭，停止后台任务")
            await wecom.stop()


async def _reload_feishu_impl(new_feishu: FeishuConfig):
    """运行时重载飞书配置"""
    global _reload_feishu_fn
    feishu = None
    for attr in ["_feishu_notifier"]:
        if hasattr(router, "_app") and hasattr(router._app.state, attr):
            feishu = getattr(router._app.state, attr)
            break
    if not feishu:
        logger.warning("无法获取 feishu_notifier 实例")
        return

    feishu.update_config(new_feishu)
    # 更新 chatid
    if new_feishu.chatid:
        feishu._cfg.chatid = new_feishu.chatid


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动/停止后台任务"""
    global _monitor_task, _camera_task, _reconnect_task, _wecom_notifier
    global _reload_printers_fn, _reload_wecom_fn, _ai_guard

    config: AppConfig = app.state.config
    _started_internally = False

    # 注册重载函数到 app.state（供 routes.py 中的 API 调用）
    app.state._reload_printers = _reload_printers_impl
    app.state._reload_wecom = _reload_wecom_impl
    app.state._reload_feishu = _reload_feishu_impl
    router._app = app  # 供重载函数访问 app

    # 如果测试已注入 mock 对象，跳过初始化
    if not hasattr(app.state, "db") or app.state.db is None:
        _started_internally = True

        # 1. 加载 Token
        token_mgr = TokenManager(config.cloud.token_file)
        token = token_mgr.load_token()
        if not token:
            raise RuntimeError(
                "未找到有效 Token，请先运行: python -m bumbo_stats.main login"
            )

        # 转换 mqtt_broker 配置: cn->china, us->global
        region = "china" if config.cloud.mqtt_broker == "cn" else "global"

        # 2. 获取用户 ID（从云端 API）
        from bambulab import BambuClient
        BambuClient.BASE_URL = "https://api.bambulab.cn" if region == "china" else "https://api.bambu.com"
        client = BambuClient(token=token)
        user_info = await asyncio.to_thread(client.get_user_info)
        uid = str(user_info.get("uid", "")) if isinstance(user_info, dict) else str(user_info)
        if not uid:
            raise RuntimeError("无法获取用户 ID，请重新登录")

        # 3. 初始化数据库
        db = Database(config.settings.db_path)
        await db.init()
        app.state.db = db

        # 4. 创建监控器（先创建以便传入回调）
        manager = PrinterManager(config.printers)
        monitor = PrintMonitor(manager, db, _broadcast)
        manager._on_status_change = monitor.handle_status_update

        # 5. 连接云端 MQTT
        await manager.connect_all(uid, token, region=region)
        app.state.printer_manager = manager
        app.state._manager = manager

        # 6. 注册打印机到数据库（通过云端 API 获取设备信息）
        try:
            devices = await asyncio.to_thread(client.get_devices)
            if devices:
                logger.info("已获取 %d 台设备信息", len(devices))
            device_map = {d.get("dev_id", ""): d for d in devices} if devices else {}
        except Exception as e:
            logger.warning("获取设备列表失败: %s", e)
            device_map = {}

        task_names = {}
        print_states = {}
        for device in devices or []:
            dev_id = device.get("dev_id", "")
            print_job = device.get("print_job")
            print_status = device.get("print_status", "")
            if print_status:
                print_states[dev_id] = print_status
            if print_job:
                try:
                    task = await asyncio.to_thread(client.get_task, print_job)
                    if task and isinstance(task, dict):
                        name = task.get("name", "")
                        if name:
                            task_names[dev_id] = name
                except Exception as e:
                    logger.debug("获取任务名称失败 %s: %s", dev_id, e)

        for c in config.printers:
            device = device_map.get(c.serial, {})
            model = device.get("dev_model_name", "")
            await db.upsert_printer(c.serial, c.name, c.ip or "", model, c.location)

        for serial in task_names.keys() | print_states.keys():
            if serial in manager._configs:
                initial_data = {}
                if serial in task_names:
                    initial_data["gcode_file"] = task_names[serial]
                if serial in print_states:
                    initial_data["gcode_state"] = print_states[serial]
                status = manager._parse_status(serial, manager._configs[serial].name, initial_data)
                manager._status_cache[serial] = status
                manager._print_data_cache[serial] = initial_data

        # 启动补录：停机前就在打印的任务结算到上次心跳时刻（而非重启时刻），
        # 避免把整个停机期误算为打印时长；心跳不存在时退化为当前时间
        last_heartbeat = await db.get_last_heartbeat()
        if last_heartbeat is not None:
            logger.info("检测到上次心跳: %s，补录结算将以此时刻封顶", last_heartbeat.isoformat())

        for serial, status in manager._status_cache.items():
            monitor._last_states[serial] = status.state
            monitor._last_gcode[serial] = status.gcode_file
            if status.state == PrinterState.RUNNING:
                await db.record_print_start(serial, status.gcode_file, settle_time=last_heartbeat)
                logger.info("启动补录: %s 正在打印 %s", serial, status.gcode_file)

        # 7. 初始化摄像头管理器
        camera = CameraManager(config.printers)
        app.state.camera_manager = camera
        app.state._camera = camera

        # 8. 初始化企业微信通知模块（可选）
        # 只要配置了 bot_id/secret 就创建实例，确保运行时重载能找到 notifier；
        # 实际后台任务的启动由 WeComNotifier.start() 根据 enabled 自行决定。
        global _wecom_notifier
        if _NOTIFY_AVAILABLE and config.wecom.bot_id and config.wecom.secret:
            _wecom_notifier = WeComNotifier(
                wecom_config=WeComConfig(
                    enabled=config.wecom.enabled,
                    bot_id=config.wecom.bot_id,
                    secret=config.wecom.secret,
                    chatid=config.wecom.chatid,
                    layer3=config.wecom.layer3,
                    progress50=config.wecom.progress50,
                    completed=config.wecom.completed,
                    error=config.wecom.error,
                ),
                printer_configs=config.printers,
                camera_manager=camera,
                printer_manager=manager,
                db=db,
                dashboard_url=config.settings.dashboard_url,
            )
            await _wecom_notifier.start()
            monitor.wecom_notifier = _wecom_notifier
            app.state._wecom_notifier = _wecom_notifier
            if config.wecom.enabled:
                logger.info("企业微信通知模块已启用")
            else:
                logger.info("企业微信通知模块已初始化（总开关 enabled=False，暂不启动后台任务）")

        # 8b. 初始化飞书通知模块（可选）
        global _feishu_notifier
        if _NOTIFY_AVAILABLE and config.feishu.enabled and config.feishu.app_id and config.feishu.app_secret:
            _feishu_notifier = FeishuNotifier(
                feishu_config=FeishuConfig(
                    app_id=config.feishu.app_id,
                    app_secret=config.feishu.app_secret,
                    chatid=config.feishu.chatid,
                    layer3=config.feishu.layer3,
                    progress50=config.feishu.progress50,
                    completed=config.feishu.completed,
                    error=config.feishu.error,
                    urgent_user_id=config.feishu.urgent_user_id,
                    urgent_on_events=list(config.feishu.urgent_on_events),
                    urgent_max_per_hour=config.feishu.urgent_max_per_hour,
                ),
                printer_configs=config.printers,
                camera_manager=camera,
                printer_manager=manager,
                db=db,
                dashboard_url=config.settings.dashboard_url,
            )
            await _feishu_notifier.start()
            monitor.feishu_notifier = _feishu_notifier
            app.state._feishu_notifier = _feishu_notifier
            logger.info("飞书通知模块已启用 (chatid=%s, urgent_users=%s, urgent_events=%s)",
                        config.feishu.chatid, config.feishu.urgent_user_id,
                        config.feishu.urgent_on_events)

        # 9. 启动后台任务
        _monitor_task = asyncio.create_task(monitor.start())
        _camera_task = asyncio.create_task(camera.capture_all_loop(config.settings.camera_interval))
        _reconnect_task = asyncio.create_task(_reconnect_loop(manager))

        # 10. 启动 AI 异常报警模块
        # 阈值持久化回调：修改内存值的同时写回 printers.yaml，
        # 这样群指令修改后重启服务仍然生效（避免仅临时生效的问题）
        def _persist_threshold(new_val: float) -> None:
            config.ai_guard.threshold = new_val
            config.save_yaml()
            logger.info("AI 检测阈值已持久化到 printers.yaml: %.2f", new_val)

        _ai_guard = AIGuardLoop(
            ai_config=config.ai_guard,
            camera_manager=camera,
            printer_manager=manager,
            ws_manager=_ws_manager,
            wecom_notifier=_wecom_notifier,
            feishu_notifier=_feishu_notifier,
            camera_interval=config.settings.camera_interval,
            on_threshold_changed=_persist_threshold,
        )
        await _ai_guard.start()
        app.state._ai_guard = _ai_guard

        # 把 ai_guard 注入到通知模块（用于支持阈值指令）
        if _wecom_notifier is not None:
            _wecom_notifier.bind_ai_guard(_ai_guard)
        if _feishu_notifier is not None:
            _feishu_notifier.bind_ai_guard(_ai_guard)

        logger.info("Bumbo Stats 已启动，监控 %d 台打印机", len(config.printers))

    yield

    # 清理（仅关闭内部启动的资源）
    if _started_internally:
        if _monitor_task:
            _monitor_task.cancel()
        if _camera_task:
            _camera_task.cancel()
        if _reconnect_task:
            _reconnect_task.cancel()
        if _ai_guard:
            _ai_guard.stop()
        if _wecom_notifier:
            await _wecom_notifier.stop()
        if _feishu_notifier:
            await _feishu_notifier.stop()
        if hasattr(app.state, "printer_manager"):
            await app.state.printer_manager.disconnect_all()
        if hasattr(app.state, "camera_manager"):
            app.state.camera_manager.stop()
        logger.info("Bumbo Stats 已关闭")


def create_app(config: AppConfig | None = None, config_path: str = "printers.yaml") -> FastAPI:
    """创建 FastAPI 应用实例"""

    if config is None:
        config = load_config(config_path)

    app = FastAPI(
        title="Bumbo Stats",
        description="拓竹3D打印机多机云端监控面板",
        version="0.5.3",
        lifespan=lifespan,
    )

    app.state.config = config
    app.state.ws_manager = _ws_manager

    pkg_dir = Path(__file__).parent.parent
    templates_dir = pkg_dir / "templates"
    static_dir = pkg_dir / "static"

    if templates_dir.exists():
        app.state.templates = Jinja2Templates(directory=str(templates_dir))

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(router)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await _ws_manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            await _ws_manager.disconnect(websocket)

    return app
