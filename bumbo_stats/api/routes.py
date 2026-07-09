"""REST API 路由"""

from fastapi import APIRouter, Request, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..config import PrinterConfig

router = APIRouter()

AUTH_COOKIE = "bumbo_auth"


class LoginRequest(BaseModel):
    password: str


# ---------------------------------------------------------------------------
# 页面路由
# ---------------------------------------------------------------------------

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
    auth_hash = request.cookies.get(AUTH_COOKIE)
    if not auth_hash or auth_hash != config.ui.admin_password_hash:
        return templates.TemplateResponse(request, "login.html", {})

    return templates.TemplateResponse(request, "settings.html", {
        "config": config,
    })


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """登录页面"""
    templates = request.app.state.templates
    config = request.app.state.config
    if not config.ui.admin_password_hash:
        return RedirectResponse(url="/settings")
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
        response.set_cookie(
            key=AUTH_COOKIE,
            value=config.ui.admin_password_hash,
            httponly=True,
            samesite="lax",
        )
        return response
    raise HTTPException(status_code=401, detail="密码错误")


@router.post("/api/auth/logout")
async def logout():
    """登出"""
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
    config.save_yaml()

    response = Response(content='{"ok":true}', media_type="application/json")
    response.set_cookie(
        key=AUTH_COOKIE,
        value=config.ui.admin_password_hash,
        httponly=True,
        samesite="lax",
    )
    return response


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def require_auth(request: Request) -> bool:
    """检查是否已登录"""
    config = request.app.state.config
    auth_hash = request.cookies.get(AUTH_COOKIE)
    return bool(auth_hash and auth_hash == config.ui.admin_password_hash)


# ---------------------------------------------------------------------------
# 设置 API
# ---------------------------------------------------------------------------

class CloudUpdate(BaseModel):
    username: str
    token_file: str = "~/.bumbo_token"
    mqtt_broker: str = "cn"


class WeComUpdate(BaseModel):
    bot_id: str
    secret: str
    chatid: str = ""
    layer3: bool = True
    completed: bool = True
    error: bool = True


class FeishuUpdate(BaseModel):
    app_id: str
    app_secret: str
    chatid: str = ""
    enabled: bool = False
    layer3: bool = True
    completed: bool = True
    error: bool = True


class PrinterUpdate(BaseModel):
    name: str
    serial: str
    ip: str = ""
    access_code: str = ""
    location: str = ""


class PrintersUpdate(BaseModel):
    printers: list[PrinterUpdate]


@router.post("/api/settings/cloud")
async def save_cloud(request: Request, body: CloudUpdate):
    """保存拓竹云配置"""
    if not require_auth(request):
        raise HTTPException(status_code=401, detail="未登录")

    config = request.app.state.config
    config.cloud.username = body.username
    config.cloud.token_file = body.token_file
    config.cloud.mqtt_broker = body.mqtt_broker
    config.save_yaml()
    return {"ok": True}


@router.post("/api/settings/wecom")
async def save_wecom(request: Request, body: WeComUpdate):
    """保存企业微信配置"""
    if not require_auth(request):
        raise HTTPException(status_code=401, detail="未登录")

    config = request.app.state.config
    config.wecom.bot_id = body.bot_id
    config.wecom.secret = body.secret
    config.wecom.chatid = body.chatid
    config.wecom.layer3 = body.layer3
    config.wecom.completed = body.completed
    config.wecom.error = body.error
    config.save_yaml()

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
    config.feishu.completed = body.completed
    config.feishu.error = body.error
    config.save_yaml()

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
    config.save_yaml()

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
    config.save_yaml()

    # 重新设置 cookie
    response = Response(content='{"ok":true}', media_type="application/json")
    response.set_cookie(
        key=AUTH_COOKIE,
        value=config.ui.admin_password_hash,
        httponly=True,
        samesite="lax",
    )
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
