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
from ..printer.wecom import WeComNotifier, WeComConfig
from .routes import router
from .websocket import ConnectionManager

logger = logging.getLogger(__name__)

# 全局引用，用于 lifespan 中取消任务
_monitor_task: asyncio.Task | None = None
_camera_task: asyncio.Task | None = None
_reconnect_task: asyncio.Task | None = None
_wecom_notifier: WeComNotifier | None = None
_ws_manager: ConnectionManager = ConnectionManager()

# 运行时重载函数（在 create_app 中注册到 app.state）
_reload_printers_fn = None
_reload_wecom_fn = None


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
    for attr in ["_manager"]:
        if hasattr(router, "_app") and hasattr(router._app.state, attr):
            manager = getattr(router._app.state, attr)
            break
    if not manager:
        logger.warning("无法获取 manager 实例")
        return

    old_serials = set(manager.serials)
    new_serials = {p.serial for p in new_printers}

    # 移除不存在的打印机
    for serial in old_serials - new_serials:
        manager.remove_printer(serial)
        logger.info("已移除打印机: %s", serial)

    # 添加新打印机
    for printer in new_printers:
        if printer.serial in new_serials - old_serials:
            manager.add_printer(printer)
            logger.info("已添加打印机: %s (%s)", printer.name, printer.serial)


async def _reload_wecom_impl(new_wecom: WeComConfig):
    """运行时重载企业微信配置"""
    global _reload_wecom_fn
    wecom = None
    for attr in ["_wecom_notifier"]:
        if hasattr(router, "_app") and hasattr(router._app.state, attr):
            wecom = getattr(router._app.state, attr)
            break
    if not wecom:
        logger.warning("无法获取 wecom_notifier 实例")
        return

    # 更新配置
    wecom._cfg = new_wecom
    logger.info("企业微信配置已重载: layer3=%s completed=%s error=%s",
                new_wecom.layer3, new_wecom.completed, new_wecom.error)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动/停止后台任务"""
    global _monitor_task, _camera_task, _reconnect_task, _wecom_notifier
    global _reload_printers_fn, _reload_wecom_fn

    config: AppConfig = app.state.config
    _started_internally = False

    # 注册重载函数到 app.state（供 routes.py 中的 API 调用）
    app.state._reload_printers = _reload_printers_impl
    app.state._reload_wecom = _reload_wecom_impl
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

        for serial, status in manager._status_cache.items():
            monitor._last_states[serial] = status.state
            monitor._last_gcode[serial] = status.gcode_file
            if status.state == PrinterState.RUNNING:
                await db.record_print_start(serial, status.gcode_file)
                logger.info("启动补录: %s 正在打印 %s", serial, status.gcode_file)

        # 7. 初始化摄像头管理器
        camera = CameraManager(config.printers)
        app.state.camera_manager = camera
        app.state._camera = camera

        # 8. 初始化企业微信通知模块（可选）
        global _wecom_notifier
        if config.wecom.bot_id and config.wecom.secret:
            _wecom_notifier = WeComNotifier(
                wecom_config=WeComConfig(
                    bot_id=config.wecom.bot_id,
                    secret=config.wecom.secret,
                    chatid=config.wecom.chatid,
                    layer3=config.wecom.layer3,
                    completed=config.wecom.completed,
                    error=config.wecom.error,
                ),
                printer_configs=config.printers,
                camera_manager=camera,
            )
            await _wecom_notifier.start()
            monitor.wecom_notifier = _wecom_notifier
            app.state._wecom_notifier = _wecom_notifier
            logger.info("企业微信通知模块已启用")

        # 9. 启动后台任务
        _monitor_task = asyncio.create_task(monitor.start())
        _camera_task = asyncio.create_task(camera.capture_all_loop(config.settings.camera_interval))
        _reconnect_task = asyncio.create_task(_reconnect_loop(manager))

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
        if _wecom_notifier:
            await _wecom_notifier.stop()
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
        version="0.5.0",
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
