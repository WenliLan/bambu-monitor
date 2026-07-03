"""WebSocket 处理器 - 管理客户端连接和消息广播"""

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class ConnectionManager:
    """WebSocket 连接管理器"""

    def __init__(self):
        self._connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            self._connections.append(websocket)
        logger.info("WebSocket 客户端连接，当前: %d", len(self._connections))

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            if websocket in self._connections:
                self._connections.remove(websocket)
        logger.info("WebSocket 客户端断开，当前: %d", len(self._connections))

    async def broadcast(self, message: dict[str, Any]):
        """向所有连接的客户端广播消息"""
        if not self._connections:
            return
        data = json.dumps(message, ensure_ascii=False, default=str)
        disconnected = []
        async with self._lock:
            for conn in self._connections:
                try:
                    await conn.send_text(data)
                except Exception:
                    disconnected.append(conn)
            for conn in disconnected:
                if conn in self._connections:
                    self._connections.remove(conn)
