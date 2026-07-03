"""REST API 路由"""

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """主面板页面"""
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "dashboard.html", {
        "printers": request.app.state.config.printers,
    })


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
