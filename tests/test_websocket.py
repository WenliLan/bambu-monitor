"""测试 WebSocket 连接管理器

测试用例:
  TC-WS-01: 连接和断开管理
  TC-WS-02: 广播消息到所有连接
  TC-WS-03: 断开后自动移除连接
"""

import pytest
from unittest.mock import AsyncMock
from bambu_monitor.api.websocket import ConnectionManager


@pytest.mark.asyncio
class TestConnectionManager:
    async def test_tc_ws_01_connect_disconnect(self):
        """TC-WS-01: 连接和断开"""
        mgr = ConnectionManager()
        ws = AsyncMock()
        await mgr.connect(ws)
        assert len(mgr._connections) == 1

        await mgr.disconnect(ws)
        assert len(mgr._connections) == 0

    async def test_tc_ws_02_broadcast(self):
        """TC-WS-02: 广播消息"""
        mgr = ConnectionManager()
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        await mgr.connect(ws1)
        await mgr.connect(ws2)

        await mgr.broadcast({"type": "test", "data": "hello"})

        ws1.send_text.assert_called_once()
        ws2.send_text.assert_called_once()

    async def test_tc_ws_03_remove_on_error(self):
        """TC-WS-03: 发送失败时移除连接"""
        mgr = ConnectionManager()
        ws_ok = AsyncMock()
        ws_fail = AsyncMock()
        ws_fail.send_text.side_effect = Exception("connection lost")

        await mgr.connect(ws_ok)
        await mgr.connect(ws_fail)

        await mgr.broadcast({"type": "test"})

        # ws_fail 应被移除
        assert ws_ok in mgr._connections
        assert ws_fail not in mgr._connections
