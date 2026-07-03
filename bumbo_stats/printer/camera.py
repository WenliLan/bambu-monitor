"""摄像头管理 - 通过本地 HTTP 获取打印机摄像头 JPEG 帧"""

import asyncio
import logging
from typing import Optional

from ..config import PrinterConfig

logger = logging.getLogger(__name__)

# X1 系列型号前缀（摄像头支持有限）
_X1_PREFIXES = ("BL-P001", "BL-P002")


class CameraManager:
    """管理多台打印机的摄像头帧获取（使用本地 IP + Access Code）"""

    def __init__(self, configs: list[PrinterConfig]):
        self._configs = {c.serial: c for c in configs}
        self._frame_cache: dict[str, bytes] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._supported: dict[str, bool] = {}

        # 判断哪些打印机支持摄像头
        for c in configs:
            # 需要同时配置 ip 和 access_code 才能获取摄像头
            has_camera = bool(c.ip and c.access_code)
            serial_prefix = c.serial[:7] if c.serial else ""
            self._supported[c.serial] = has_camera and serial_prefix not in _X1_PREFIXES

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

    async def _capture_one(self, serial: str):
        """获取单台打印机的摄像头帧

        Args:
            serial: 打印机序列号
        """
        config = self._configs.get(serial)
        if not config or not config.ip or not config.access_code:
            return
        try:
            def _get_frame():
                from bambulab import JPEGFrameStream
                with JPEGFrameStream(config.ip, config.access_code) as stream:
                    return stream.get_frame()

            frame = await asyncio.to_thread(_get_frame)
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

    def is_supported(self, serial: str) -> bool:
        """检查打印机是否支持摄像头

        Args:
            serial: 打印机序列号

        Returns:
            True 表示支持摄像头
        """
        return self._supported.get(serial, False)
