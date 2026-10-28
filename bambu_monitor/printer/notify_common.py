"""企业微信 / 飞书机器人通知的公共逻辑

被 wecom.py 与 feishu.py 共享，包含:
  - 通知事件常量（layer3/progress50/completed/error/ai_alarm）
  - 状态 emoji + gcode 归一化
  - 摄像头帧捕获（缓存 + 直连 fallback）
  - 监控总览 HTML / 月度 SVG 折线图（与网页面板四个区块一致）
  - 无头浏览器长截图渲染（playwright 不可用时回退）
  - 群聊指令关键词 / 冷却 / 解析
  - AI 阈值指令通用处理（chatid + ai_guard 由调用方注入）

注意: 此模块不绑定任何具体 SDK（不 import wecom_aibot_sdk / lark_oapi），
所有平台相关的发送动作由 wecom.py / feishu.py 通过回调注入。
"""

import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import Optional

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 事件常量
# ----------------------------------------------------------------------

EVENT_LAYER3 = "layer3"
EVENT_PROGRESS50 = "progress50"
EVENT_COMPLETED = "completed"
EVENT_ERROR = "error"
EVENT_AI_ALARM = "ai_alarm"

ALL_EVENTS = (EVENT_LAYER3, EVENT_PROGRESS50, EVENT_COMPLETED, EVENT_ERROR, EVENT_AI_ALARM)

# 通知标题（emoji + 事件类型），共享给 wecom markdown / feishu 富文本
EVENT_TITLES = {
    EVENT_LAYER3: "🖨️ 打印已到达第 3 层",
    EVENT_PROGRESS50: "⏳ 打印进度已过半",
    EVENT_COMPLETED: "✅ 打印已完成",
    EVENT_ERROR: "🚨 打印机异常",
    EVENT_AI_ALARM: "🤖 AI 检测到打印异常",
}

# 每次通知发送的截图数量和间隔（降低挤出机遮挡概率）
FRAMES_PER_NOTIFICATION = 2
FRAME_INTERVAL = 30.0  # 秒

# ----------------------------------------------------------------------
# 状态图标 + gcode 归一化
# ----------------------------------------------------------------------

_STATE_EMOJI = {
    "RUNNING": "🟢",
    "IDLE": "⚪",
    "PAUSE": "⏸️",
    "FINISH": "✅",
    "FAILED": "❌",
    "OFFLINE": "⚫",
    "UNKNOWN": "❓",
}


def state_emoji(state: str) -> str:
    """获取状态对应的 emoji（未知状态返回 None）"""
    return _STATE_EMOJI.get(state)


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


# ----------------------------------------------------------------------
# 摄像头帧捕获
# ----------------------------------------------------------------------

async def capture_camera_frames(
    camera_manager,
    serial: str,
    frames_per_notification: int = FRAMES_PER_NOTIFICATION,
    frame_interval: float = FRAME_INTERVAL,
) -> list[bytes]:
    """获取多张摄像头帧，间隔 frame_interval 秒，降低遮挡概率

    Args:
        camera_manager: CameraManager 实例（优先使用其缓存）
        serial: 打印机序列号
        frames_per_notification: 获取帧数
        frame_interval: 帧间隔（秒）

    Returns:
        JPEG bytes 列表（可能为空）
    """
    frames: list[bytes] = []
    for i in range(frames_per_notification):
        if i > 0:
            await asyncio.sleep(frame_interval)
        frame = await capture_camera_one(camera_manager, serial)
        if frame:
            frames.append(frame)
    return frames


async def capture_camera_one(camera_manager, serial: str) -> Optional[bytes]:
    """获取单张摄像头帧：优先从 CameraManager 缓存，未命中则主动抓帧

    Args:
        camera_manager: CameraManager 实例
        serial: 打印机序列号

    Returns:
        JPEG bytes 或 None
    """
    if not camera_manager:
        return None

    # 优先从缓存获取（低延迟）
    frame = await camera_manager.get_frame(serial)
    if frame:
        return frame

    # 缓存没有，通过 CameraManager 主动抓帧（自动按机型分流 JPEG/RTSPS 协议）
    try:
        frame = await camera_manager.capture_fresh_frame(serial)
        return frame if isinstance(frame, (bytes, bytearray)) else None
    except Exception as e:
        logger.warning("直连摄像头失败 %s: %s", serial, e)
        return None


# ----------------------------------------------------------------------
# 群聊指令（关键词 + 冷却 + 解析）
# ----------------------------------------------------------------------

# 指令定义：每个指令关联一个或多个关键词
COMMANDS = (
    ("threshold", ("阈值", "threshold")),  # 动态修改 AI 异常检测阈值
    ("help", ("帮助", "help")),
    ("overview", ("总览",)),
    ("status", ("状态",)),
    ("snapshot", ("截图", "监控")),
)

COMMAND_COOLDOWN = {
    "snapshot": 10.0,
    "overview": 10.0,
    "status": 5.0,
    "help": 3.0,
    "threshold": 3.0,
}


@dataclass
class ParsedCommand:
    """指令解析结果"""
    command: str           # 指令名（threshold / help / overview / status / snapshot）
    raw_text: str          # 原始消息文本
    numeric_value: Optional[float] = None  # 数字参数（仅 threshold 提取）


def parse_command(text: str) -> Optional[ParsedCommand]:
    """从消息文本中解析指令

    Args:
        text: 消息原始文本（可能含 @机器人 提及）

    Returns:
        解析结果（command + raw_text + numeric_value），不匹配任何指令时返回 None
    """
    if not text:
        return None
    for cmd, keywords in COMMANDS:
        if any(kw in text for kw in keywords):
            numeric_value = None
            if cmd == "threshold":
                m = re.search(r"(\d+(?:\.\d+)?)", text)
                if m:
                    try:
                        numeric_value = float(m.group(1))
                    except ValueError:
                        numeric_value = None
            return ParsedCommand(command=cmd, raw_text=text, numeric_value=numeric_value)
    return None


class CommandCooldown:
    """群聊指令冷却控制（避免指令被高频触发）

    每个指令维护一个"上次触发时间戳"，距今小于冷却时间时直接拒绝。
    同一指令两次触发之间的间隔应 >= 冷却时间。
    """

    def __init__(self):
        self._last_ts: dict[str, float] = {}

    def check(self, cmd: str) -> bool:
        """检查指令是否可触发（未冷却则返回 True 并刷新时间戳）

        Returns:
            True 表示允许触发（同时刷新冷却时间戳）
            False 表示处于冷却中（忽略本次）
        """
        cooldown = COMMAND_COOLDOWN.get(cmd, 5.0)
        now = time.time()
        if now - self._last_ts.get(cmd, 0.0) < cooldown:
            logger.info("指令 [%s] 冷却中，忽略: %.1fs 前刚触发",
                        cmd, now - self._last_ts[cmd])
            return False
        self._last_ts[cmd] = now
        return True

    def remaining(self, cmd: str) -> float:
        """距离下次可触发还需等待的秒数（0 表示已可触发）"""
        cooldown = COMMAND_COOLDOWN.get(cmd, 5.0)
        elapsed = time.time() - self._last_ts.get(cmd, 0.0)
        return max(0.0, cooldown - elapsed)


# ----------------------------------------------------------------------
# 阈值指令（chatid + ai_guard 由调用方注入）
# ----------------------------------------------------------------------

def build_threshold_message(
    ai_guard,
    numeric_value: Optional[float],
    dashboard_url: str = "",
) -> str:
    """构建阈值指令响应文本（平台无关，调用方负责发送）

    Args:
        ai_guard: AIGuardLoop 实例（用于读取/修改阈值）
        numeric_value: 新阈值（None 表示仅查询当前值）
        dashboard_url: 监控面板 URL（用于响应结尾链接）；留空则不显示链接，
                       由调用方从 settings.dashboard_url 配置注入（不再硬编码）

    Returns:
        渲染好的 markdown 文本（wecom）/ 纯文本（feishu）
    """
    if ai_guard is None:
        return "⚠️ AI 检测模块未启动，无法修改阈值"

    old = ai_guard.threshold
    if old is None:
        return "⚠️ AI 检测器初始化未完成，请稍后重试"

    if numeric_value is None:
        # 仅查询
        return (
            f"## 🎚️ 当前 AI 检测阈值\n\n"
            f"- **阈值**: {old:.2f}\n\n"
            f"_用法: 发送 `阈值 0.6` 修改_"
        )

    prev = ai_guard.set_threshold(numeric_value)
    if prev < 0:
        return "⚠️ AI 检测器不可用，修改失败"

    current = ai_guard.threshold
    msg = (
        f"## 🎚️ AI 检测阈值已更新\n\n"
        f"- **旧值**: {prev:.2f}\n"
        f"- **新值**: {current:.2f}\n"
    )
    if abs(numeric_value - current) > 0.001:
        msg += (
            f"_（输入 {numeric_value} 超出 0~1 范围，已自动截断为 {current:.2f}）_\n"
        )
    msg += "\n_注: 已持久化到 printers.yaml，重启服务后仍然生效_\n"
    if dashboard_url:
        msg += f"\n[打开监控面板]({dashboard_url})"
    return msg


# ----------------------------------------------------------------------
# 监控总览 HTML / SVG 折线图 / 长截图渲染
# ----------------------------------------------------------------------

def build_monthly_svg(monthly_usage: list) -> str:
    """用 SVG 绘制近 30 天产能饱和度折线图（自包含，无需 JS）"""
    if not monthly_usage or not any(m.points for m in monthly_usage):
        return '<div class="no-img">暂无产能数据</div>'

    colors = ["#10b981", "#3b82f6", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899"]
    dates = sorted({p.date for m in monthly_usage for p in m.points})
    n = len(dates)
    max_pct = max(
        (p.utilization_percent for m in monthly_usage for p in m.points),
        default=0,
    )
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
        parts.append(
            f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" '
            f'stroke="#374151" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{x0 - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'fill="#9ca3af" font-size="11">{v:.0f}%</text>'
        )
    # x 轴日期标签（首/中/尾）
    for i in {0, n // 2, n - 1}:
        parts.append(
            f'<text x="{px(i):.1f}" y="{y1 + 18}" text-anchor="middle" '
            f'fill="#9ca3af" font-size="11">{str(dates[i])[5:]}</text>'
        )
    # 每台打印机的折线
    legends = []
    for idx, m in enumerate(monthly_usage):
        color = colors[idx % len(colors)]
        pts = " ".join(
            f"{px(dates.index(p.date)):.1f},{py(p.utilization_percent):.1f}"
            for p in sorted(m.points, key=lambda p: p.date)
        )
        if pts:
            parts.append(
                f'<polyline points="{pts}" fill="none" stroke="{color}" '
                f'stroke-width="2"/>'
            )
        legends.append(
            f'<span class="legend-item">'
            f'<span class="dot" style="background:{color}"></span>{m.printer_name}</span>'
        )

    return (
        f'<svg viewBox="0 0 760 235" style="width:100%;height:auto">{"".join(parts)}</svg>'
        f'<div class="legend">{"".join(legends)}</div>'
    )


def build_overview_html(
    printer_names: dict[str, str],
    printer_locations: dict[str, str],
    statuses: dict,
    snapshots: dict[str, bytes],
    today_usage: list = None,
    monthly_usage: list = None,
) -> str:
    """生成自包含的监控总览 HTML，内容与网页面板四个区块一致

    Args:
        printer_names: {serial: name}
        printer_locations: {serial: location}
        statuses: {serial: PrinterStatus}
        snapshots: {serial: JPEG bytes} 实时截图
        today_usage: 今日产能列表（每台打印机一行）
        monthly_usage: 近 30 天产能列表
    """
    today_usage = today_usage or []
    monthly_usage = monthly_usage or []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 区块1：📷 实时监控（摄像头截图）
    cam_cards = []
    for serial, name in printer_names.items():
        frame = snapshots.get(serial)
        img_b64 = base64.b64encode(frame).decode() if frame else ""
        img_html = (
            f'<img src="data:image/jpeg;base64,{img_b64}" alt="{escape(name)}">'
            if img_b64
            else '<div class="no-img">无摄像头画面</div>'
        )
        loc = printer_locations.get(serial, "")
        st = statuses.get(serial)
        state_val = st.state.value if st else "OFFLINE"
        cam_cards.append(f"""
    <div class="card">
      <div class="card-head"><h3>{escape(name)}</h3><span class="loc">{escape(loc)}</span></div>
      <div class="cam-wrap">{img_html}<span class="state-tag">{state_val}</span></div>
    </div>""")

    # 区块2：📊 打印机状态（与网页卡片字段一致）
    stat_cards = []
    for serial, name in printer_names.items():
        st = statuses.get(serial)
        if st:
            state_val = st.state.value
            emoji = state_emoji(state_val) or "❓"
            progress = st.progress
            h, m = divmod(st.remaining_time, 60)
            eta = (
                (f"{h}小时{m}分钟" if h else f"{m}分钟")
                if st.remaining_time > 0
                else "--"
            )
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

    # 区块4：📈 近 30 天产能趋势（SVG 折线图）
    chart_html = build_monthly_svg(monthly_usage)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bambu Monitor 监控总览 {now}</title>
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
<h1>🖨️ Bambu Monitor 监控总览</h1>
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


async def render_overview_image(html: str) -> Optional[bytes]:
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
                        full_page=True, type="jpeg", quality=quality,
                    )
                    if len(img) <= 1_900_000:
                        break
                return img
            finally:
                await browser.close()
    except Exception as e:
        logger.warning("总览长截图渲染失败: %s，回退为 HTML 文件发送", e)
        return None


# ----------------------------------------------------------------------
# 帮助指令文本（共享给 wecom markdown / feishu 富文本）
# ----------------------------------------------------------------------

HELP_TEXT = """🤖 支持的指令

@机器人 发送含以下关键词的消息即可触发：

- **截图** / **监控** — 发送所有打印机实时摄像头截图
- **状态** — 文字回复所有打印机当前状态（层数/进度/剩余时间/温度）
- **总览** — 生成监控面板完整长截图（含状态+截图+产能图表）
- **阈值** + 数字（如 `阈值 0.6`） — 动态修改 AI 异常检测阈值（0~1），仅 `阈值` 不带数字则查询当前值，修改会持久化到 yaml
- **帮助** — 显示本消息

另外会自动推送：打印到第 3 层、打印完成、HMS 异常告警、AI 异常报警"""