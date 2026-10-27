"""REST API 路由"""

import asyncio
import secrets
import time

from fastapi import APIRouter, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..config import PrinterConfig

router = APIRouter()

AUTH_COOKIE = "bumbo_auth"
SESSION_TTL = 7 * 24 * 3600  # 会话有效期（秒）


class LoginRequest(BaseModel):
    password: str


# ---------------------------------------------------------------------------
# 会话管理（P2-14：Cookie 保存随机 token，不再回显口令哈希）
# ---------------------------------------------------------------------------

def _session_store(request: Request) -> dict:
    store = getattr(request.app.state, "_sessions", None)
    if store is None:
        store = {}
        request.app.state._sessions = store
    return store


def create_session(request: Request) -> str:
    """创建随机会话 token（进程内存，重启失效）"""
    token = secrets.token_urlsafe(32)
    _session_store(request)[token] = time.time() + SESSION_TTL
    return token


def clear_sessions(request: Request) -> None:
    _session_store(request).clear()


def set_auth_cookie(response: Response, request: Request) -> None:
    response.set_cookie(
        key=AUTH_COOKIE,
        value=create_session(request),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
    )


# ---------------------------------------------------------------------------
# 页面路由
# ---------------------------------------------------------------------------

@router.get("/healthz")
async def healthz(request: Request):
    """公开健康检查（P2-4）

    始终返回 200（只要进程还能响应），内容区分 ok/degraded，
    便于外部探针与人工自检在不登录的情况下确认版本/Token/MQTT 状态。
    """
    app = request.app
    cloud = dict(getattr(app.state, "cloud_state", {}) or {})
    manager = getattr(app.state, "printer_manager", None)
    printers = []
    if manager is not None:
        try:
            mqtt_status = manager.cloud_status()
        except Exception:
            mqtt_status = {}
        try:
            statuses = manager.get_all_statuses()
        except Exception:
            statuses = {}
        for serial, st in statuses.items():
            info = mqtt_status.get(serial, {}) if isinstance(mqtt_status, dict) else {}
            printers.append({
                "name": getattr(st, "name", ""),
                "serial": serial,
                "state": getattr(getattr(st, "state", None), "value", str(getattr(st, "state", ""))),
                "mqtt_connected": info.get("connected"),
                "last_message_age": info.get("last_message_age"),
            })
    ai = getattr(app.state, "_ai_guard", None)
    ai_status = ai.status() if ai is not None and hasattr(ai, "status") else {}
    authenticated = bool(cloud.get("authenticated"))
    return JSONResponse({
        "status": "ok" if authenticated else "degraded",
        "version": __version__,
        "cloud": {
            "token_loaded": bool(cloud.get("token_loaded")),
            "authenticated": authenticated,
            "region": cloud.get("region", ""),
            "error": cloud.get("error", ""),
            "detail": cloud.get("detail", ""),
        },
        "printers": printers,
        "ai": {
            "enabled": ai_status.get("enabled"),
            "available": ai_status.get("available"),
            "threshold": ai_status.get("threshold"),
            "last_check_at": ai_status.get("last_check_at"),
        },
    })


@router.get("/api/status")
async def api_status(request: Request):
    """运行状态汇总（需登录；可通过 /healthz 获取公开子集）"""
    app = request.app
    manager = getattr(app.state, "printer_manager", None)
    ai = getattr(app.state, "_ai_guard", None)
    mqtt_status = manager.cloud_status() if manager is not None else {}
    return {
        "version": __version__,
        "cloud": dict(getattr(app.state, "cloud_state", {}) or {}),
        "printers": mqtt_status,
        "ai": ai.status() if ai is not None and hasattr(ai, "status") else {},
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """主面板页面"""
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "dashboard.html", {
        "printers": request.app.state.config.printers,
    })


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """设置页面（需要登录）"""
    templates = request.app.state.templates
    config = request.app.state.config

    # 检查是否已设置密码
    if not config.ui.admin_password_hash:
        # 首次使用：引导设置密码
        return templates.TemplateResponse(request, "setup.html", {})

    # 检查登录状态
    if not require_auth(request):
        return templates.TemplateResponse(request, "login.html", {})

    return templates.TemplateResponse(request, "settings.html", {
        "config": config,
    })


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """登录页面（未设置密码时也返回 200，便于自检，P2-4）"""
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "login.html", {})


# ---------------------------------------------------------------------------
# 认证 API
# ---------------------------------------------------------------------------

@router.post("/api/auth/login")
async def login(request: Request, body: LoginRequest):
    """登录验证"""
    config = request.app.state.config
    if config.verify_password(body.password):
        response = Response(content='{"ok":true}', media_type="application/json")
        set_auth_cookie(response, request)
        return response
    raise HTTPException(status_code=401, detail="密码错误")


@router.post("/api/auth/logout")
async def logout(request: Request):
    """登出（销毁当前会话 token）"""
    token = request.cookies.get(AUTH_COOKIE)
    if token:
        _session_store(request).pop(token, None)
    response = Response(content='{"ok":true}', media_type="application/json")
    response.delete_cookie(AUTH_COOKIE)
    return response


@router.post("/api/auth/setup")
async def setup_password(request: Request, body: LoginRequest):
    """首次设置管理员密码"""
    config = request.app.state.config
    if config.ui.admin_password_hash:
        raise HTTPException(status_code=400, detail="已设置过密码")

    if len(body.password) < 4:
        raise HTTPException(status_code=400, detail="密码至少4位")

    config.set_password(body.password)
    # 只写回 ui 段，保留磁盘上手工编辑的打印机/注释结构（P2-12）
    config.save_yaml(sections=["ui"])

    response = Response(content='{"ok":true}', media_type="application/json")
    set_auth_cookie(response, request)
    return response


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def require_auth(request: Request) -> bool:
    """检查是否已登录（校验随机会话 token，P2-14）"""
    token = request.cookies.get(AUTH_COOKIE)
    if not token:
        return False
    store = _session_store(request)
    expires = store.get(token)
    if not expires:
        return False
    if expires < time.time():
        store.pop(token, None)
        return False
    return True


# ---------------------------------------------------------------------------
# 设置 API
# ---------------------------------------------------------------------------

class CloudUpdate(BaseModel):
    token_file: str = "~/.bumbo_token"
    mqtt_broker: str = "cn"


class WeComUpdate(BaseModel):
    enabled: bool = True
    bot_id: str
    secret: str
    chatid: str = ""
    layer3: bool = True
    progress50: bool = True
    completed: bool = True
    error: bool = True


class FeishuUpdate(BaseModel):
    app_id: str
    app_secret: str
    chatid: str = ""
    enabled: bool = False
    layer3: bool = True
    progress50: bool = True
    completed: bool = True
    error: bool = True
    urgent_user_id: str = ""
    urgent_on_events: list[str] = Field(
        default_factory=lambda: ["error"],
        description="触发应用内加急的事件列表",
    )
    urgent_max_per_hour: int = 10


class PrinterUpdate(BaseModel):
    name: str
    serial: str
    ip: str = ""
    access_code: str = ""
    location: str = ""


class PrintersUpdate(BaseModel):
    printers: list[PrinterUpdate]


class CloudLoginRequest(BaseModel):
    account: str
    password: str
    region: str = ""


class CloudVerifyRequest(BaseModel):
    code: str


class FeishuChatsRequest(BaseModel):
    app_id: str = ""
    app_secret: str = ""


async def _activate_cloud(request: Request, token: str, region: str) -> bool:
    """保存 Token/区域并热连接云端（网页登录成功后调用）"""
    from ..auth import TokenManager
    from ..cloud_api import region_from_broker

    config = request.app.state.config
    token_mgr = getattr(request.app.state, "token_manager", None)
    if token_mgr is None:
        token_mgr = TokenManager(config.cloud.token_file)
        request.app.state.token_manager = token_mgr
    # Token 已由 TokenManager 落盘；区域写回配置，重启后仍用同一区域
    config.cloud.mqtt_broker = "cn" if region == "china" else "us"
    config.save_yaml(sections=["cloud"])

    reload_fn = getattr(request.app.state, "_reload_cloud", None)
    if reload_fn is None:
        return False
    return bool(await reload_fn(token, region_from_broker(config.cloud.mqtt_broker)))


@router.post("/api/settings/cloud/login")
async def cloud_login(request: Request, body: CloudLoginRequest):
    """网页端登录拓竹云（P1-1）

    第一步提交账号+密码；若云端要求验证码，返回 need_code=true，
    再调用 `/api/settings/cloud/verify` 提交短信/邮件/MFA 验证码。
    """
    if not require_auth(request):
        raise HTTPException(status_code=401, detail="未登录")

    from ..auth import TokenManager
    from ..cloud_api import region_from_broker

    config = request.app.state.config
    token_mgr = getattr(request.app.state, "token_manager", None)
    if token_mgr is None:
        token_mgr = TokenManager(config.cloud.token_file)
        request.app.state.token_manager = token_mgr

    region = region_from_broker(body.region or config.cloud.mqtt_broker)
    try:
        result = await asyncio.to_thread(
            token_mgr.begin_login, body.account, body.password, region
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"登录失败: {e}")

    if result.get("status") == "ok":
        request.app.state._cloud_login_pending = None
        connected = await _activate_cloud(request, result["token"], region)
        if not connected:
            raise HTTPException(status_code=502, detail="Token 已保存，但连接拓竹云失败，请稍后重试")
        return {"ok": True, "need_code": False, "region": region}

    request.app.state._cloud_login_pending = result
    is_tfa = result.get("status") == "need_tfa"
    return {
        "ok": True,
        "need_code": True,
        "need_tfa": is_tfa,
        "hint": result.get("hint") if not is_tfa else "请输入 MFA 验证码",
        "region": region,
    }


@router.post("/api/settings/cloud/verify")
async def cloud_verify(request: Request, body: CloudVerifyRequest):
    """提交拓竹云验证码完成登录"""
    if not require_auth(request):
        raise HTTPException(status_code=401, detail="未登录")

    from ..auth import TokenManager

    pending = getattr(request.app.state, "_cloud_login_pending", None)
    if not pending:
        raise HTTPException(status_code=400, detail="没有待完成的登录流程，请先提交账号密码")

    token_mgr = getattr(request.app.state, "token_manager", None)
    if token_mgr is None:
        token_mgr = TokenManager(request.app.state.config.cloud.token_file)
        request.app.state.token_manager = token_mgr

    try:
        token = await asyncio.to_thread(token_mgr.complete_login, pending, body.code)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"验证码验证失败: {e}")

    request.app.state._cloud_login_pending = None
    region = pending.get("region", "global")
    connected = await _activate_cloud(request, token, region)
    if not connected:
        raise HTTPException(status_code=502, detail="Token 已保存，但连接拓竹云失败，请稍后重试")
    return {"ok": True, "region": region}


@router.post("/api/settings/feishu/chats")
async def feishu_chats(request: Request, body: FeishuChatsRequest):
    """用表单里的 app_id/app_secret 列出机器人所在群聊（P2-15）"""
    if not require_auth(request):
        raise HTTPException(status_code=401, detail="未登录")

    config = request.app.state.config
    app_id = body.app_id or config.feishu.app_id
    app_secret = body.app_secret or config.feishu.app_secret
    if not app_id or not app_secret:
        raise HTTPException(status_code=400, detail="请先填写 App ID / App Secret")

    notifier = getattr(request.app.state, "_feishu_notifier", None)
    try:
        if notifier is not None:
            chats = await notifier.fetch_group_chats(app_id, app_secret)
        else:
            from ..printer.feishu import FeishuConfig, FeishuNotifier
            tmp = FeishuNotifier(
                FeishuConfig(app_id=app_id, app_secret=app_secret), config.printers
            )
            chats = await tmp.fetch_group_chats()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "chats": chats}


@router.post("/api/settings/cloud")
async def save_cloud(request: Request, body: CloudUpdate):
    """保存拓竹云配置"""
    if not require_auth(request):
        raise HTTPException(status_code=401, detail="未登录")

    config = request.app.state.config
    config.cloud.token_file = body.token_file
    config.cloud.mqtt_broker = body.mqtt_broker
    config.save_yaml(sections=["cloud"])
    return {"ok": True}


@router.post("/api/settings/wecom")
async def save_wecom(request: Request, body: WeComUpdate):
    """保存企业微信配置"""
    if not require_auth(request):
        raise HTTPException(status_code=401, detail="未登录")

    config = request.app.state.config
    config.wecom.enabled = body.enabled
    config.wecom.bot_id = body.bot_id
    config.wecom.secret = body.secret
    config.wecom.chatid = body.chatid
    config.wecom.layer3 = body.layer3
    config.wecom.progress50 = body.progress50
    config.wecom.completed = body.completed
    config.wecom.error = body.error
    config.save_yaml(sections=["wecom"])

    # 通知模块重载配置
    if hasattr(request.app.state, "_reload_wecom"):
        await request.app.state._reload_wecom(config.wecom)

    return {"ok": True}


@router.post("/api/settings/feishu")
async def save_feishu(request: Request, body: FeishuUpdate):
    """保存飞书配置"""
    if not require_auth(request):
        raise HTTPException(status_code=401, detail="未登录")

    config = request.app.state.config
    config.feishu.app_id = body.app_id
    config.feishu.app_secret = body.app_secret
    config.feishu.chatid = body.chatid
    config.feishu.enabled = body.enabled
    config.feishu.layer3 = body.layer3
    config.feishu.progress50 = body.progress50
    config.feishu.completed = body.completed
    config.feishu.error = body.error
    # P2-17：只覆盖请求体里显式出现的加急字段，避免旧表单少传时被清零
    provided = getattr(body, "model_fields_set", set())
    if "urgent_user_id" in provided:
        config.feishu.urgent_user_id = body.urgent_user_id
    if "urgent_on_events" in provided:
        config.feishu.urgent_on_events = body.urgent_on_events
    if "urgent_max_per_hour" in provided:
        config.feishu.urgent_max_per_hour = body.urgent_max_per_hour
    # 只写回 feishu 段，保留磁盘上其他段的手工编辑（P2-12）
    config.save_yaml(sections=["feishu"])

    # 通知模块重载配置
    if hasattr(request.app.state, "_reload_feishu"):
        await request.app.state._reload_feishu(config.feishu)

    return {"ok": True}


@router.post("/api/settings/wecom/test")
async def test_wecom(request: Request):
    """发送企业微信测试消息"""
    if not require_auth(request):
        raise HTTPException(status_code=401, detail="未登录")

    if not hasattr(request.app.state, "_wecom_notifier") or not request.app.state._wecom_notifier:
        raise HTTPException(status_code=400, detail="企业微信未启用")

    try:
        success = await request.app.state._wecom_notifier.send_test_message()
        if success:
            return {"ok": True, "message": "测试消息发送成功"}
        else:
            raise HTTPException(status_code=400, detail="测试消息发送失败")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"发送失败: {str(e)}")


@router.post("/api/settings/feishu/test")
async def test_feishu(request: Request):
    """发送飞书测试消息"""
    if not require_auth(request):
        raise HTTPException(status_code=401, detail="未登录")

    if not hasattr(request.app.state, "_feishu_notifier") or not request.app.state._feishu_notifier:
        raise HTTPException(status_code=400, detail="飞书未启用")

    try:
        success = await request.app.state._feishu_notifier.send_test_message()
        if success:
            return {"ok": True, "message": "测试消息发送成功"}
        else:
            raise HTTPException(status_code=400, detail="测试消息发送失败")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"发送失败: {str(e)}")


@router.post("/api/settings/printers")
async def save_printers(request: Request, body: PrintersUpdate):
    """保存打印机配置"""
    if not require_auth(request):
        raise HTTPException(status_code=401, detail="未登录")

    config = request.app.state.config
    config.printers = [
        PrinterConfig(**p.model_dump()) for p in body.printers
    ]
    config.save_yaml(sections=["printers"])

    # 通知 PrinterManager 重载打印机配置
    if hasattr(request.app.state, "_reload_printers"):
        await request.app.state._reload_printers(config.printers)

    return {"ok": True}


@router.post("/api/settings/password")
async def change_password(request: Request, body: LoginRequest):
    """修改管理员密码"""
    if not require_auth(request):
        raise HTTPException(status_code=401, detail="未登录")

    if len(body.password) < 4:
        raise HTTPException(status_code=400, detail="密码至少4位")

    config = request.app.state.config
    config.set_password(body.password)
    config.save_yaml(sections=["ui"])

    # 修改密码后作废所有旧会话，并给当前客户端签发新 token
    clear_sessions(request)
    response = Response(content='{"ok":true}', media_type="application/json")
    set_auth_cookie(response, request)
    return response


@router.get("/api/printers")
async def get_all_printers(request: Request):
    """获取所有打印机当前状态（从缓存）"""
    manager = request.app.state.printer_manager
    statuses = manager.get_all_statuses()
    return {s: st.model_dump(mode="json") for s, st in statuses.items()}


@router.get("/api/printers/{serial}")
async def get_printer(request: Request, serial: str):
    """获取单台打印机状态（从缓存）"""
    manager = request.app.state.printer_manager
    status = manager.get_status(serial)
    return status.model_dump(mode="json")


@router.get("/api/printers/{serial}/camera")
async def get_camera_frame(request: Request, serial: str):
    """获取摄像头最新帧 (JPEG)"""
    camera = request.app.state.camera_manager
    frame = await camera.get_frame(serial)
    if not frame:
        return Response(status_code=404, content="Camera frame not available")
    return Response(content=frame, media_type="image/jpeg")


@router.get("/api/usage/today")
async def get_today_usage(request: Request):
    """获取今日所有打印机的产能饱和度"""
    db = request.app.state.db
    manager = request.app.state.printer_manager
    items = await db.get_today_usage_all(manager.printer_names)
    return [item.model_dump(mode="json") for item in items]


@router.get("/api/usage/monthly")
async def get_monthly_usage(request: Request, days: int = 30):
    """获取所有打印机近N天每日产能数据（用于折线图）"""
    db = request.app.state.db
    manager = request.app.state.printer_manager
    monthly = await db.get_monthly_usage_all(manager.printer_names, days=days)
    return [m.model_dump(mode="json") for m in monthly]


@router.get("/api/sessions/{serial}")
async def get_sessions(request: Request, serial: str, limit: int = 20):
    """获取打印任务历史"""
    db = request.app.state.db
    sessions = await db.get_sessions(serial, limit=limit)
    return [s.model_dump(mode="json") for s in sessions]


# ---------------------------------------------------------------------------
# AI 推理 API
# ---------------------------------------------------------------------------

@router.post("/api/ai/detect/{serial}")
async def ai_detect(request: Request, serial: str):
    """手动触发单帧 AI 推理

    返回推理结果和摄像头截图，不触发报警流程。
    每次推理都会保存图片到本地（由 ai_guard 统一处理）：
    - 异常: data/alerts_review/
    - 正常: data/normal_review/
    """
    import base64

    camera = request.app.state.camera_manager
    ai_guard = getattr(request.app.state, "_ai_guard", None)

    if not ai_guard or not ai_guard.is_available:
        raise HTTPException(status_code=503, detail="AI detector not available")

    # 获取摄像头帧
    frame = await camera.get_frame(serial)
    if not frame:
        raise HTTPException(status_code=503, detail="Camera frame not available")

    # 执行推理（图片保存在 ai_guard.detect_single 中处理）
    result = await ai_guard.detect_single(frame, serial)

    # 附加打印机名称
    manager = request.app.state.printer_manager
    status = manager.get_status(serial)
    result["printer_name"] = status.name
    result["printer_state"] = status.state.value

    # 附加摄像头截图 (base64)
    result["image"] = base64.b64encode(frame).decode()

    return result
