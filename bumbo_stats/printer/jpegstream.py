"""A1 / P1 系列打印机 JPEG 帧流（本地实现，去除对 bambulab SDK 的依赖）

协议：TLS 连接 6000 端口，发送 0x3000 认证包（用户名 bblp + access_code），
之后每帧为 16 字节头（payload_size/itrack/flags/reserved）+ JPEG 数据。

与 bambu-lab-cloud-api 的实现相比：
- 校验帧长度上限，避免异常数据导致超大内存分配；
- 所有失败统一抛 VideoStreamError，便于上层降级为“无画面”；
- 保留上下文管理器用法，CameraManager 的调用方式不变。
"""

from __future__ import annotations

import logging
import socket
import ssl
import struct
from typing import Generator, Optional

logger = logging.getLogger(__name__)

FRAME_START = b"\xff\xd8"  # JPEG SOI
FRAME_END = b"\xff\xd9"    # JPEG EOI

# 1280x720 JPEG 通常 < 500KB；给 16MB 上限足以覆盖高码率机型
MAX_FRAME_BYTES = 16 * 1024 * 1024


class VideoStreamError(RuntimeError):
    """摄像头帧流连接或读取失败"""


class JPEGFrameStream:
    """A1 / P1 系列打印机的 TCP 6000 JPEG 帧流"""

    def __init__(
        self,
        printer_ip: str,
        access_code: str,
        port: int = 6000,
        timeout: float = 10.0,
    ):
        self.printer_ip = printer_ip
        self.access_code = access_code
        self.port = port
        self.timeout = timeout
        self.socket: Optional[ssl.SSLSocket] = None

    def connect(self) -> None:
        if not self.printer_ip:
            raise VideoStreamError("未配置打印机 IP")
        if not self.access_code:
            raise VideoStreamError("未配置 Access Code")

        raw = None
        try:
            raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw.settimeout(self.timeout)
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            self.socket = context.wrap_socket(raw)
            raw = None  # 所有权已转移给 SSLSocket
            self.socket.settimeout(self.timeout)
            self.socket.connect((self.printer_ip, self.port))
            self._authenticate()
        except VideoStreamError:
            self.disconnect()
            raise
        except (OSError, ssl.SSLError, ValueError) as e:
            self.disconnect()
            raise VideoStreamError(
                f"连接摄像头失败 {self.printer_ip}:{self.port}: {e}"
            ) from e
        finally:
            if raw is not None:
                raw.close()

    def _authenticate(self) -> None:
        if not self.socket:
            raise VideoStreamError("未连接")
        username = b"bblp"
        password = self.access_code.encode("ascii", errors="ignore")
        packet = struct.pack("<I", 0x40)          # payload size
        packet += struct.pack("<I", 0x3000)       # type: auth
        packet += struct.pack("<I", 0)            # flags
        packet += struct.pack("<I", 0)            # reserved
        packet += username.ljust(32, b"\x00")
        packet += password.ljust(32, b"\x00")
        try:
            self.socket.sendall(packet)
        except OSError as e:
            raise VideoStreamError(f"发送认证包失败: {e}") from e

    def _recv_exact(self, size: int) -> bytes:
        if not self.socket:
            raise VideoStreamError("未连接")
        data = bytearray()
        while len(data) < size:
            try:
                chunk = self.socket.recv(size - len(data))
            except socket.timeout as e:
                raise VideoStreamError("读取摄像头数据超时") from e
            except OSError as e:
                raise VideoStreamError(f"读取摄像头数据失败: {e}") from e
            if not chunk:
                raise VideoStreamError("摄像头连接已关闭")
            data.extend(chunk)
        return bytes(data)

    def get_frame(self) -> bytes:
        if not self.socket:
            raise VideoStreamError("未连接，请先调用 connect()")

        header = self._recv_exact(16)
        payload_size, _itrack, _flags, _reserved = struct.unpack("<IIII", header)
        if payload_size <= 0 or payload_size > MAX_FRAME_BYTES:
            raise VideoStreamError(f"非法帧长度: {payload_size}")

        image_data = self._recv_exact(payload_size)
        if not image_data.startswith(FRAME_START):
            raise VideoStreamError("帧数据缺少 JPEG 起始标记")
        if not image_data.endswith(FRAME_END):
            raise VideoStreamError("帧数据缺少 JPEG 结束标记")
        return image_data

    def stream_frames(self) -> Generator[bytes, None, None]:
        while True:
            try:
                yield self.get_frame()
            except VideoStreamError as e:
                logger.debug("JPEG 帧流结束 %s: %s", self.printer_ip, e)
                return

    def disconnect(self) -> None:
        sock, self.socket = self.socket, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self) -> "JPEGFrameStream":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.disconnect()
