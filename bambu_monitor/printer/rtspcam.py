"""RTSP 摄像头取帧 - 通过 ffmpeg 子进程从 RTSPS 322 端口机型抓取单帧 JPEG

拓竹机型摄像头分两种协议（与 Bambu Studio / Bambuddy 社区实现一致）：
- A1 / A1 mini / P1P / P1S：TCP 6000 端口 JPEG 帧流（bambulab SDK JPEGFrameStream）
- X1 全系 / P2S / P2P / X2D / H2D / H2S 等：RTSPS 322 端口（本模块，需 ffmpeg）

ffmpeg 查找顺序：环境变量 BUMBO_FFMPEG_EXE → PATH → imageio-ffmpeg（pip 安装 [rtsp] extra）。
找不到可用 ffmpeg 时相关函数返回 None 并给出一次性 warning，不影响其他功能。
"""

import logging
import os
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# RTSP 抓帧超时（秒）：含 TLS 握手 + DESCRIBE + 首个关键帧
RTSP_FRAME_TIMEOUT = 15.0

# ffmpeg 缺失只提示一次，避免抓取循环刷屏
_ffmpeg_missing_warned = False


def _reset_warn_state():
    """测试用：重置一次性告警标记"""
    global _ffmpeg_missing_warned
    _ffmpeg_missing_warned = False


def find_ffmpeg() -> Optional[str]:
    """查找可用的 ffmpeg 可执行文件路径

    Returns:
        ffmpeg 路径；不可用时 None
    """
    # 1. 环境变量显式指定
    env_exe = os.environ.get("BUMBO_FFMPEG_EXE", "").strip()
    if env_exe:
        if os.path.isfile(env_exe):
            return env_exe
        logger.warning("BUMBO_FFMPEG_EXE 指向的文件不存在: %s", env_exe)

    # 2. 系统 PATH
    exe = shutil.which("ffmpeg")
    if exe:
        return exe

    # 3. imageio-ffmpeg 自带的静态二进制（pip install "bambu_monitor[rtsp]"）
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def rtsp_frame_url(ip: str, access_code: str) -> str:
    """构造 Bambu RTSPS 直播流地址（凭据内嵌）"""
    return f"rtsps://bblp:{access_code}@{ip}:322/streaming/live/1"


def _NO_WINDOW() -> int:
    """Windows 下不弹控制台窗口；其他平台返回 0"""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def capture_rtsp_frame(ip: str, access_code: str) -> Optional[bytes]:
    """从 RTSPS 322 机型抓取一张 JPEG 帧

    Args:
        ip: 打印机局域网 IP
        access_code: 摄像头接入码

    Returns:
        JPEG bytes；失败或 ffmpeg 不可用时 None
    """
    ffmpeg_exe = find_ffmpeg()
    if not ffmpeg_exe:
        global _ffmpeg_missing_warned
        if not _ffmpeg_missing_warned:
            _ffmpeg_missing_warned = True
            logger.warning(
                "未找到 ffmpeg，RTSPS 机型（X1/P2S/P2P/X2D/H2D 等）摄像头不可用；"
                '安装方式任选其一：系统安装 ffmpeg 并加入 PATH、设置环境变量 '
                'BUMBO_FFMPEG_EXE、或 pip install "bambu_monitor[rtsp]"'
            )
        return None

    cmd = [
        ffmpeg_exe, "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", rtsp_frame_url(ip, access_code),
        "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg",
        "pipe:1",
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, timeout=RTSP_FRAME_TIMEOUT,
            creationflags=_NO_WINDOW(),
        )
        if out.returncode == 0 and out.stdout:
            return bytes(out.stdout)
        err = (out.stderr or b"").decode("utf-8", "replace").strip()
        logger.debug("RTSPS 抓帧失败 %s: %s", ip, err[:200])
        return None
    except subprocess.TimeoutExpired:
        logger.debug("RTSPS 抓帧超时 %s（检查打印机与主机同网段、access_code 是否正确）", ip)
        return None
    except Exception as e:
        logger.debug("RTSPS 抓帧异常 %s: %s", ip, e)
        return None
