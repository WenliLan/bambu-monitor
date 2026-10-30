"""FastAPI 应用创建与生命周期管理"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..models import PrinterState
from ..auth import TokenManager
from ..cloud_api import BambuCloudClient, CloudAPIError, region_from_broker
from ..config import AppConfig, PrinterConfig, load_config
from ..database import Database
from ..lan_discovery import LanDiscovery
from ..printer.camera import CameraManager
from ..printer.manager import PrinterManager
from ..printer.monitor import PrintMonitor
try:
    # 通知能力可选：未安装 extras 时降级为不启用（pip install "bambu_monitor[notify]"）
    from ..printer.wecom import WeComNotifier, WeComConfig
    from ..printer.feishu import FeishuNotifier, FeishuConfig
    _NOTIFY_AVAILABLE = True
except ImportError as _e:  # noqa: B902
    WeComNotifier = WeComConfig = FeishuNotifier = FeishuConfig = None  # type: ignore[assignment]
    _NOTIFY_AVAILABLE = False
    logging.getLogger(__name__).warning(
        "通知模块不可用（缺少依赖: %s），监控面板正常运行；"
        "如需企微/飞书通知请安装: pip install \"bambu_monitor[notify]\"", _e,
    )
from ..printer.ai_guard import AIGuardLoop
from .routes import router, require_auth
from .websocket import ConnectionManager

logger = logging.getLogger(__name__)

# 全局引用，用于 lifespan 中取消任务
_monitor_task: asyncio.Task | None = None
_camera_task: asyncio.Task | None = None
_reconnect_task: asyncio.Task | None = None
_cloud_retry_task: asyncio.Task | None = None
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


async def _backfill_from_cloud(client, config: AppConfig, db: Database,
                               manager: PrinterManager, monitor: PrintMonitor):
    """启动/重连时用云端 REST 数据补录设备型号、任务名与初始状态"""
    devices: list[dict] = []
    try:
        devices = await asyncio.to_thread(client.get_devices)
        if devices:
            logger.info("已获取 %d 台设备信息", len(devices))
    except Exception as e:
        logger.warning("获取设备列表失败: %s", e)

    device_map = {d.get("dev_id", ""): d for d in devices}
    task_names: dict[str, str] = {}
    print_states: dict[str, str] = {}
    for device in devices:
        dev_id = device.get("dev_id", "")
        print_job = device.get("print_job")
        print_status = device.get("print_status", "")
        if print_status:
            print_states[dev_id] = print_status
        if print_job:
            try:
                task = await asyncio.to_thread(client.get_task, print_job)
                name = task.get("name", "") if isinstance(task, dict) else ""
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


async def _attach_cloud(app: FastAPI, config: AppConfig, manager: PrinterManager,
                        token: str, region: str):
    """用 Token 连接拓竹云并补录设备信息；失败时抛出 CloudAPIError

    成功后会更新 app.state.cloud_state / cloud_client。
    """
    client = BambuCloudClient(token=token, region=region)
    try:
        user_info = await asyncio.to_thread(client.get_user_info)
        uid = (
            str(user_info.get("uid", ""))
            if isinstance(user_info, dict)
            else str(user_info or "")
        ).strip()
        if not uid:
            raise CloudAPIError("云端未返回用户 ID（uid）")
    except Exception:
        client.close()
        raise

    await manager.connect_all(uid, token, region=region)
    try:
        await _backfill_from_cloud(
            client, config, app.state.db, manager, app.state._monitor
        )
    except Exception as e:
        logger.warning("云端设备信息补录失败（实时状态不受影响）: %s", e)

    app.state.cloud_client = client
    app.state.cloud_state.update(
        token_loaded=True, authenticated=True, uid=uid, region=region,
        error="", detail="",
    )
    return client


async def _cloud_retry_loop(app: FastAPI, config: AppConfig, manager: PrinterManager,
                            token_mgr: TokenManager, region: str, interval: float = 60.0):
    """云端不可用时定期重试（网络恢复 / 用户重新登录后自动接上）"""
    while True:
        try:
            await asyncio.sleep(interval)
            state = getattr(app.state, "cloud_state", {})
            if state.get("authenticated"):
                continue
            token = token_mgr.load_token()
            if not token:
                continue
            previous_error = state.get("error")
            try:
                await _attach_cloud(app, config, manager, token, region)
                logger.info("拓竹云已恢复连接（uid=%s）", state.get("uid"))
            except Exception as e:
                state["error"] = "token_invalid" if getattr(e, "is_auth_error", False) else "cloud_unreachable"
                state["detail"] = str(e)
                # 只在错误类型变化时打 INFO，避免每分钟刷屏
                if previous_error != state["error"]:
                    logger.warning("拓竹云重试失败（%s）: %s", state["error"], e)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("云端重试循环异常: %s", e)


async def _reload_cloud_impl(token: str, region: str):
    """网页端登录成功后热连接云端（P1-1）"""
    app = router._app
    config: AppConfig = app.state.config
    manager: PrinterManager = app.state.printer_manager
    # 断掉旧会话（如有），避免重复 client 与旧凭据残留
    await manager.disconnect_all()
    app.state.cloud_state.update(
        token_loaded=True, authenticated=False, uid="", region=region,
        error="", detail="正在连接…",
    )
    try:
        await _attach_cloud(app, config, manager, token, region)
        logger.info("网页端登录成功，拓竹云已连接（region=%s）", region)
        return True
    except Exception as e:
        code = "token_invalid" if getattr(e, "is_auth_error", False) else "cloud_unreachable"
        app.state.cloud_state.update(error=code, detail=str(e))
        logger.error("网页端登录后连接云端失败（%s）: %s", code, e)
        return False


async def _reload_printers_impl(new_printers: list[PrinterConfig]):
    """运行时重载打印机配置"""
    app = router._app
    manager = getattr(app.state, "_manager", None)
    camera = getattr(app.state, "_camera", None)
    db = getattr(app.state, "db", None)
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
    added = new_serials - old_serials
    for printer in new_printers:
        if printer.serial in added:
            await manager.add_printer(printer)
            if db is not None:
                # print_sessions 有指向 printers(serial) 的外键引用，热新增必须先登记
                try:
                    await db.upsert_printer(
                        printer.serial, printer.name, printer.ip or "", "", printer.location or ""
                    )
                except Exception as e:
                    logger.warning("登记新打印机到数据库失败 %s: %s", printer.serial, e)
            logger.info("已添加打印机: %s (%s)", printer.name, printer.serial)

    # 重载摄像头配置（即时更新 access_code 等）
    if camera:
        camera.reload_configs(new_printers)

    # 通知模块按新机型列表同步状态跟踪表（面板增删机后无需重启）
    for notifier in (_wecom_notifier, _feishu_notifier):
        if notifier is not None and hasattr(notifier, "sync_printers"):
            try:
                notifier.sync_printers(new_printers)
            except Exception as e:
                logger.warning("通知模块同步打印机列表失败: %s", e)


def _build_wecom(app: FastAPI, cfg, config: AppConfig):
    """按当前配置创建企业微信通知实例（不启动后台任务）"""
    if not _NOTIFY_AVAILABLE or not (cfg.bot_id and cfg.secret):
        return None
    try:
        return WeComNotifier(
            wecom_config=WeComConfig(
                enabled=cfg.enabled,
                bot_id=cfg.bot_id,
                secret=cfg.secret,
                chatid=cfg.chatid,
                layer3=cfg.layer3,
                progress50=cfg.progress50,
                completed=cfg.completed,
                error=cfg.error,
            ),
            printer_configs=config.printers,
            camera_manager=getattr(app.state, "camera_manager", None),
            printer_manager=getattr(app.state, "printer_manager", None),
            db=getattr(app.state, "db", None),
            dashboard_url=config.settings.dashboard_url,
        )
    except Exception as e:
        logger.error("创建企业微信通知实例失败: %s", e)
        return None


def _build_feishu(app: FastAPI, cfg, config: AppConfig):
    """按当前配置创建飞书通知实例（不启动后台任务）"""
    if not _NOTIFY_AVAILABLE or not (cfg.app_id and cfg.app_secret):
        return None
    try:
        return FeishuNotifier(
            feishu_config=FeishuConfig(
                app_id=cfg.app_id,
                app_secret=cfg.app_secret,
                chatid=cfg.chatid,
                layer3=cfg.layer3,
                progress50=cfg.progress50,
                completed=cfg.completed,
                error=cfg.error,
                urgent_user_id=cfg.urgent_user_id,
                urgent_on_events=list(cfg.urgent_on_events),
                urgent_max_per_hour=cfg.urgent_max_per_hour,
            ),
            printer_configs=config.printers,
            camera_manager=getattr(app.state, "camera_manager", None),
            printer_manager=getattr(app.state, "printer_manager", None),
            db=getattr(app.state, "db", None),
            dashboard_url=config.settings.dashboard_url,
        )
    except Exception as e:
        logger.error("创建飞书通知实例失败: %s", e)
        return None


def _bind_ai_threshold_guard(notifier) -> None:
    """把当前 AI 守卫注入通知模块（群指令改阈值用）"""
    guard = getattr(router._app.state, "_ai_guard", None) if getattr(router, "_app", None) else None
    if notifier is not None and guard is not None and hasattr(notifier, "bind_ai_guard"):
        notifier.bind_ai_guard(guard)


async def _reload_wecom_impl(new_wecom: WeComConfig):
    """运行时重载企业微信配置（实例缺失时按新凭据懒创建，P2-16 同类缺口）"""
    global _wecom_notifier
    app = router._app
    wecom = getattr(app.state, "_wecom_notifier", None)
    if wecom is None:
        wecom = _build_wecom(app, new_wecom, app.state.config)
        if wecom is None:
            logger.warning("企业微信凭据不完整，未创建通知实例")
            return
        _wecom_notifier = wecom
        app.state._wecom_notifier = wecom
        _bind_ai_threshold_guard(wecom)
        logger.info("企业微信通知实例已按新配置创建")

    changed = wecom.update_config(new_wecom)
    if new_wecom.enabled and (changed or not getattr(wecom, "_running", False)):
        logger.info("企业微信总开关打开，启动后台任务")
        await wecom.start()
    elif not new_wecom.enabled and (changed or getattr(wecom, "_running", False)):
        logger.info("企业微信总开关关闭，停止后台任务")
        await wecom.stop()

    monitor = getattr(app.state, "_monitor", None)
    if monitor is not None:
        monitor.wecom_notifier = wecom if new_wecom.enabled else None


async def _reload_feishu_impl(new_feishu: FeishuConfig):
    """运行时重载飞书配置

    - 实例缺失时按新凭据懒创建（P2-16：面板勾选启用后无需重启）
    - 按 enabled 启停后台任务
    - 重新注入 monitor / AIGuardLoop（AI 报警用的是构造时引用）
    """
    global _feishu_notifier
    app = router._app
    feishu = getattr(app.state, "_feishu_notifier", None)
    if feishu is None:
        feishu = _build_feishu(app, new_feishu, app.state.config)
        if feishu is None:
            logger.warning("飞书凭据不完整，未创建通知实例")
            return
        _feishu_notifier = feishu
        app.state._feishu_notifier = feishu
        _bind_ai_threshold_guard(feishu)
        logger.info("飞书通知实例已按新配置创建")

    feishu.update_config(new_feishu)
    if new_feishu.chatid:
        feishu._cfg.chatid = new_feishu.chatid

    running = bool(getattr(feishu, "_running", False))
    if new_feishu.enabled and not running:
        await feishu.start()
        logger.info("飞书通知模块已启用 (chatid=%s)", new_feishu.chatid or "(未配置)")
    elif not new_feishu.enabled and running:
        await feishu.stop()
        logger.info("飞书通知模块已停用")

    monitor = getattr(app.state, "_monitor", None)
    guard = getattr(app.state, "_ai_guard", None)
    target = feishu if new_feishu.enabled else None
    if monitor is not None:
        monitor.feishu_notifier = target
    if guard is not None:
        guard._feishu = target


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动/停止后台任务"""
    global _monitor_task, _camera_task, _reconnect_task, _cloud_retry_task
    global _wecom_notifier, _feishu_notifier
    global _reload_printers_fn, _reload_wecom_fn, _reload_feishu_fn, _ai_guard

    config: AppConfig = app.state.config
    _started_internally = False

    # 注册重载函数到 app.state（供 routes.py 中的 API 调用）
    app.state._reload_printers = _reload_printers_impl
    app.state._reload_wecom = _reload_wecom_impl
    app.state._reload_feishu = _reload_feishu_impl
    app.state._reload_cloud = _reload_cloud_impl
    router._app = app  # 供重载函数访问 app

    # 如果测试已注入 mock 对象，跳过初始化
    if not hasattr(app.state, "db") or app.state.db is None:
        _started_internally = True
        config_path = config.config_path or "printers.yaml"
        region = region_from_broker(config.cloud.mqtt_broker)

        token_mgr = TokenManager(config.cloud.token_file)
        app.state.token_manager = token_mgr
        app.state.cloud_state = {
            "token_loaded": False,
            "authenticated": False,
            "uid": "",
            "region": region,
            "error": "",
            "detail": "",
        }

        # 1. 数据库先起来：即使云端不可用，面板与历史数据仍可用
        db = Database(config.settings.db_path)
        await db.init()
        app.state.db = db

        # 2. 管理器与监控器（云端连接在第 4 步按凭据可用性决定）
        manager = PrinterManager(config.printers)
        monitor = PrintMonitor(manager, db, _broadcast)
        manager._on_status_change = monitor.handle_status_update
        app.state.printer_manager = manager
        app.state._manager = manager
        app.state._monitor = monitor

        # 3. 摄像头（本地协议，与云端无关）
        camera = CameraManager(config.printers)
        app.state.camera_manager = camera
        app.state._camera = camera

        # 4. 云端凭据：缺失/失效时降级启动，而不是拒绝启动（P1-1）
        token = token_mgr.load_token()
        app.state.cloud_state["token_loaded"] = bool(token)
        if not token:
            app.state.cloud_state["error"] = "no_token"
            logger.error(
                "未找到拓竹云 Token，面板以离线模式启动（状态/摄像头/AI 不可用）。"
                "请运行 `bambu-monitor login -c %s`，或在网页「设置 → 拓竹云」中登录。",
                config_path,
            )
        else:
            try:
                await _attach_cloud(app, config, manager, token, region)
                logger.info(
                    "拓竹云已连接: uid=%s region=%s",
                    app.state.cloud_state["uid"], region,
                )
            except Exception as e:
                code = (
                    "token_invalid"
                    if getattr(e, "is_auth_error", False)
                    else "cloud_unreachable"
                )
                app.state.cloud_state.update(error=code, detail=str(e))
                logger.error(
                    "拓竹云连接失败（%s），面板以离线模式启动（每 60 秒自动重试）: %s",
                    code, e,
                )

        # 5. 初始化企业微信通知模块（可选）
        # 只要配置了 bot_id/secret 就创建实例，确保运行时重载能找到 notifier；
        # 实际后台任务的启动由 WeComNotifier.start() 根据 enabled 自行决定。
        global _wecom_notifier
        _wecom_notifier = _build_wecom(app, config.wecom, config)
        if _wecom_notifier is not None:
            await _wecom_notifier.start()
            app.state._wecom_notifier = _wecom_notifier
            if config.wecom.enabled:
                monitor.wecom_notifier = _wecom_notifier
                logger.info("企业微信通知模块已启用")
            else:
                logger.info("企业微信通知模块已初始化（总开关 enabled=False，暂不启动后台任务）")

        # 5b. 初始化飞书通知模块（可选）
        # 与企微一致：有凭据就建实例（便于运行时启用/测试），enabled 只决定是否启动（P2-16）
        global _feishu_notifier
        _feishu_notifier = _build_feishu(app, config.feishu, config)
        if _feishu_notifier is not None:
            app.state._feishu_notifier = _feishu_notifier
            if config.feishu.enabled:
                await _feishu_notifier.start()
                monitor.feishu_notifier = _feishu_notifier
                logger.info(
                    "飞书通知模块已启用 (chatid=%s, urgent_users=%s, urgent_events=%s)",
                    config.feishu.chatid, config.feishu.urgent_user_id,
                    config.feishu.urgent_on_events,
                )
            else:
                logger.info("飞书通知模块已初始化（enabled=False，暂不启动后台任务）")

        # 6. 启动后台任务（与云端是否连上无关）
        _monitor_task = asyncio.create_task(monitor.start())
        _camera_task = asyncio.create_task(camera.capture_all_loop(config.settings.camera_interval))
        _reconnect_task = asyncio.create_task(_reconnect_loop(manager))
        _cloud_retry_task = asyncio.create_task(
            _cloud_retry_loop(app, config, manager, token_mgr, region)
        )

        # 7. 启动 AI 异常报警模块
        # 阈值持久化回调：修改内存值的同时写回 printers.yaml，
        # 这样群指令修改后重启服务仍然生效（避免仅临时生效的问题）
        def _persist_threshold(new_val: float) -> None:
            config.ai_guard.threshold = new_val
            try:
                config.save_yaml(sections=["ai_guard"])
            except Exception as e:
                logger.error("AI 阈值持久化失败: %s", e)
            logger.info("AI 检测阈值已持久化到 printers.yaml: %.2f", new_val)

        _ai_guard = AIGuardLoop(
            ai_config=config.ai_guard,
            camera_manager=camera,
            printer_manager=manager,
            ws_manager=_ws_manager,
            wecom_notifier=_wecom_notifier,
            feishu_notifier=_feishu_notifier if config.feishu.enabled else None,
            camera_interval=config.settings.camera_interval,
            on_threshold_changed=_persist_threshold,
        )
        # 模型加载放到后台，避免阻塞服务启动约 12 秒（P3-10）
        await _ai_guard.start(background=True)
        app.state._ai_guard = _ai_guard

        # 把 ai_guard 注入到通知模块（用于支持阈值指令）
        if _wecom_notifier is not None:
            _wecom_notifier.bind_ai_guard(_ai_guard)
        if _feishu_notifier is not None:
            _feishu_notifier.bind_ai_guard(_ai_guard)

        # 8. 局域网发现（可选，默认关闭）：仅用于「从云端导入设备」时补全
        #    云端接口不返回的 IP；绑定失败只降级，不影响服务启动
        lan_disc = LanDiscovery(
            enabled=config.lan_discovery.enabled,
            port=config.lan_discovery.port,
            ttl=config.lan_discovery.ttl,
            probe_interval=config.lan_discovery.probe_interval,
            multicast_dns=config.lan_discovery.multicast_dns,
        )
        await lan_disc.start()
        app.state.lan_discovery = lan_disc

        logger.info("Bambu Monitor 已启动，监控 %d 台打印机", len(config.printers))

    yield

    # 清理（仅关闭内部启动的资源）
    if _started_internally:
        lan_disc = getattr(app.state, "lan_discovery", None)
        if lan_disc is not None:
            await lan_disc.stop()
        for task in (_monitor_task, _camera_task, _reconnect_task, _cloud_retry_task):
            if task:
                task.cancel()
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
        if hasattr(app.state, "cloud_client") and app.state.cloud_client:
            app.state.cloud_client.close()
        logger.info("Bambu Monitor 已关闭")


def create_app(config: AppConfig | None = None, config_path: str = "printers.yaml") -> FastAPI:
    """创建 FastAPI 应用实例"""

    if config is None:
        config = load_config(config_path)

    app = FastAPI(
        title="Bambu Monitor",
        description="拓竹3D打印机多机云端监控面板",
        version=__version__,
        lifespan=lifespan,
    )

    app.state.config = config
    app.state.ws_manager = _ws_manager

    # 无需登录即可访问的路径（P2-14：其余页面/API 均需会话）
    public_paths = {
        "/healthz",
        "/login",
        "/api/auth/login",
        "/api/auth/setup",
        "/api/auth/logout",
        "/favicon.ico",
    }

    @app.middleware("http")
    async def auth_middleware(request, call_next):
        path = request.url.path
        if path.startswith("/static/"):
            response = await call_next(request)
            # 模板引用带 ?v=<版本号>，可安全长缓存；升级后 URL 变化自然失效（P2-21）
            response.headers.setdefault("Cache-Control", "public, max-age=86400")
            return response
        if path in public_paths:
            return await call_next(request)

        cfg = request.app.state.config
        configured = bool(getattr(cfg.ui, "admin_password_hash", ""))
        if not configured:
            # 尚未设置管理员密码：只放行设置页完成初始化
            if path in ("/settings", "/login"):
                return await call_next(request)
            if path.startswith("/api/"):
                return JSONResponse(
                    {"detail": "尚未设置管理员密码，请先访问 /settings"},
                    status_code=401,
                )
            return RedirectResponse(url="/settings", status_code=303)

        if require_auth(request):
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse({"detail": "未登录"}, status_code=401)
        if path in ("/settings", "/login"):
            return await call_next(request)  # 页面自身渲染登录/初始化表单
        return RedirectResponse(url="/login", status_code=303)

    pkg_dir = Path(__file__).parent.parent
    templates_dir = pkg_dir / "templates"
    static_dir = pkg_dir / "static"

    if templates_dir.exists():
        app.state.templates = Jinja2Templates(directory=str(templates_dir))
        # 静态资源版本串唯一来源：包版本号（P2-21）
        app.state.templates.env.globals["asset_ver"] = __version__

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(router)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        # WebSocket 不受 HTTP 中间件保护，需单独校验会话（P2-14）
        if not require_auth(websocket):
            await websocket.close(code=1008)
            return
        await _ws_manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            await _ws_manager.disconnect(websocket)

    return app
