"""摄像头管理 - 按机型自动分流摄像头协议

拓竹机型摄像头分两派（与 Bambu Studio / Bambuddy 社区确认一致）：
- TCP 6000 JPEG 帧流：A1 / A1 mini / P1P / P1S（bambulab SDK JPEGFrameStream）
- RTSPS 322 端口：X1 全系 / P2S / P2P / X2D / H2D / H2S 等（ffmpeg 取帧，见 rtspcam）

不靠序列号前缀判型（新型号前缀未知易漏），首次采集时探测端口：
6000 可达→JPEG，否则 322 可达且 ffmpeg 可用→RTSPS，结果按序列号缓存。
"""

import asyncio
import logging
import socket
from typing import Optional

from ..config import PrinterConfig
from .rtspcam import find_ffmpeg, capture_rtsp_frame

logger = logging.getLogger(__name__)

# 摄像头协议常量
PROTO_JPEG = "jpeg"      # TCP 6000，A1/P1 系
PROTO_RTSPS = "rtsps"    # RTSPS 322，X1/P2/X2/H2 系

# 端口探测超时（秒）
PROBE_TIMEOUT = 1.5


def _tcp_reachable(ip: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    """探测目标端口是否可建立 TCP 连接"""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


class CameraManager:
    """管理多台打印机的摄像头帧获取（使用本地 IP + Access Code，协议自动分流）"""

    def __init__(self, configs: list[PrinterConfig]):
        self._configs = {c.serial: c for c in configs}
        self._frame_cache: dict[str, bytes] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._supported: dict[str, bool] = {}
        self._protocol: dict[str, Optional[str]] = {}  # serial -> 探测到的协议（None=未定）

        # 判断哪些打印机支持摄像头（配了 ip+access_code 即可，协议由探测决定）
        for c in configs:
            self._supported[c.serial] = bool(c.ip and c.access_code)

    async def capture_all_loop(self, interval: float = 3.0):
        """后台循环获取所有打印机摄像头帧

        Args:
            interval: 帧刷新间隔（秒）
        """
        self._running = True
        logger.info("摄像头捕获循环启动，间隔: %ss", interval)
        while self._running:
            tasks = []
            for serial in self._configs:
                if self._supported.get(serial, False):
                    tasks.append(self._capture_one(serial))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    def stop(self):
        self._running = False

    def _detect_protocol(self, config: PrinterConfig) -> Optional[str]:
        """探测并缓存该打印机的摄像头协议

        Returns:
            PROTO_JPEG / PROTO_RTSPS；两端口均不可达时 None（下个周期重试）
        """
        serial = config.serial
        cached = self._protocol.get(serial)
        if cached:
            return cached

        if _tcp_reachable(config.ip, 6000):
            self._protocol[serial] = PROTO_JPEG
            logger.info("摄像头协议探测: %s -> JPEG(TCP 6000)", serial[-6:])
            return PROTO_JPEG
        if _tcp_reachable(config.ip, 322):
            # RTSPS 需要 ffmpeg；不可用时不缓存，便于对方装好 ffmpeg 后自动启用
            if find_ffmpeg():
                self._protocol[serial] = PROTO_RTSPS
                logger.info("摄像头协议探测: %s -> RTSPS(322, ffmpeg)", serial[-6:])
                return PROTO_RTSPS
            return None
        return None

    async def _grab_frame(self, config: PrinterConfig) -> Optional[bytes]:
        """按探测到的协议抓取一帧（在线程里执行阻塞操作）"""
        proto = self._detect_protocol(config)
        if proto == PROTO_JPEG:
            def _get():
                from bambulab import JPEGFrameStream
                with JPEGFrameStream(config.ip, config.access_code) as stream:
                    return stream.get_frame()
            return await asyncio.to_thread(_get)
        if proto == PROTO_RTSPS:
            return await asyncio.to_thread(
                capture_rtsp_frame, config.ip, config.access_code
            )
        return None

    async def _capture_one(self, serial: str):
        """获取单台打印机的摄像头帧

        Args:
            serial: 打印机序列号
        """
        config = self._configs.get(serial)
        if not config or not config.ip or not config.access_code:
            return
        try:
            frame = await self._grab_frame(config)
            if frame and isinstance(frame, (bytes, bytearray)):
                async with self._lock:
                    self._frame_cache[serial] = bytes(frame)
        except Exception as e:
            # 摄像头不可用或网络问题，静默处理
            logger.debug("摄像头帧获取失败 %s: %s", serial, e)

    async def get_frame(self, serial: str) -> Optional[bytes]:
        """获取指定打印机的最新摄像头帧

        Args:
            serial: 打印机序列号

        Returns:
            JPEG 帧数据，无可用帧时返回 None
        """
        async with self._lock:
            return self._frame_cache.get(serial)

    async def capture_fresh_frame(self, serial: str) -> Optional[bytes]:
        """主动抓取一张新摄像头帧（绕过缓存，确保拿到的是新帧）

        用于 AI 检测等需要明确时间间隔的场景，避免从缓存拿到相同帧。

        Args:
            serial: 打印机序列号

        Returns:
            新抓取的 JPEG 帧数据，失败时返回 None
        """
        config = self._configs.get(serial)
        if not config or not config.ip or not config.access_code:
            return None
        try:
            frame = await self._grab_frame(config)
            if frame and isinstance(frame, (bytes, bytearray)):
                f = bytes(frame)
                # 同步刷新缓存，避免其他模块读到旧帧
                async with self._lock:
                    self._frame_cache[serial] = f
                return f
            return None
        except Exception as e:
            logger.debug("主动抓取摄像头帧失败 %s: %s", serial, e)
            return None

    def is_supported(self, serial: str) -> bool:
        """检查打印机是否支持摄像头

        Args:
            serial: 打印机序列号

        Returns:
            True 表示支持摄像头
        """
        return self._supported.get(serial, False)

    def reload_configs(self, configs: list[PrinterConfig]):
        """重新加载打印机配置（用于运行时更新 access_code 等）

        Args:
            configs: 新的打印机配置列表
        """
        old_serials = set(self._configs.keys())
        new_configs = {c.serial: c for c in configs}

        # 更新配置
        self._configs.update(new_configs)

        # 移除不存在的打印机
        for serial in old_serials - set(new_configs.keys()):
            self._configs.pop(serial, None)
            self._frame_cache.pop(serial, None)
            self._supported.pop(serial, None)
            self._protocol.pop(serial, None)

        # 重新判断支持状态（协议由首次采集时探测）
        for c in configs:
            self._supported[c.serial] = bool(c.ip and c.access_code)
            # 配置可能换了 ip，重置旧探测结果
            self._protocol.pop(c.serial, None)

        logger.info("摄像头配置已重载，共 %d 台打印机", len(self._configs))
