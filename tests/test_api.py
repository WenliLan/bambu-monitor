"""测试 API 路由集成 - test_api.py

测试用例:
  TC-API-01: GET / 返回 Dashboard HTML
  TC-API-02: GET /api/printers 返回状态字典
  TC-API-03: GET /api/usage/today 返回今日产能
  TC-API-04: GET /api/usage/monthly 返回月度数据
  TC-API-05: GET /api/printers/{serial} 返回单台状态
  TC-API-06: GET /api/printers/{serial}/camera 无帧时返回404
  TC-API-07: GET /api/sessions/{serial} 返回打印历史
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import date

from fastapi.testclient import TestClient

from bumbo_stats.config import AppConfig, PrinterConfig, AppSettings, CloudConfig
from bumbo_stats.models import PrinterStatus, PrinterState, TodayUsageItem, MonthlyUsage, MonthlyUsagePoint


def _create_test_app():
    """创建一个带 mock 的测试应用"""
    config = AppConfig(
        cloud=CloudConfig(username="test@test.com"),
        printers=[
            PrinterConfig(name="TestP1", serial="S001", ip="1.2.3.4", access_code="0000"),
        ],
        settings=AppSettings(db_path=":memory:"),
    )

    # mock 打印机管理器（云端模式：get_all_statuses 返回缓存）
    mock_manager = MagicMock()
    mock_manager.get_all_statuses.return_value = {
        "S001": PrinterStatus(serial="S001", name="TestP1", state=PrinterState.IDLE),
    }
    mock_manager.get_status.return_value = PrinterStatus(
        serial="S001", name="TestP1", state=PrinterState.IDLE,
    )
    mock_manager.printer_names = {"S001": "TestP1"}

    # mock 摄像头
    mock_camera = AsyncMock()
    mock_camera.get_frame.return_value = None

    # mock 数据库
    mock_db = AsyncMock()
    mock_db.get_today_usage_all.return_value = [
        TodayUsageItem(
            printer_serial="S001", printer_name="TestP1",
            total_print_seconds=3600, utilization_percent=4.2,
            print_time_display="1h 00m",
        ),
    ]
    mock_db.get_monthly_usage_all.return_value = [
        MonthlyUsage(
            printer_serial="S001", printer_name="TestP1",
            points=[MonthlyUsagePoint(date=date.today(), utilization_percent=4.2)],
        ),
    ]
    mock_db.get_sessions.return_value = []

    from bumbo_stats.api.app import create_app
    app = create_app(config=config)

    # 注入 mock 对象
    app.state.printer_manager = mock_manager
    app.state.camera_manager = mock_camera
    app.state.db = mock_db

    return app


@pytest.fixture
def client():
    app = _create_test_app()
    return TestClient(app)


class TestDashboardRoute:
    def test_tc_api_01_dashboard_html(self, client):
        """TC-API-01: GET / 返回 HTML 页面"""
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Bumbo Stats" in resp.text
        assert "camera-grid" in resp.text


class TestPrinterRoutes:
    def test_tc_api_02_get_all_printers(self, client):
        """TC-API-02: GET /api/printers"""
        resp = client.get("/api/printers")
        assert resp.status_code == 200
        data = resp.json()
        assert "S001" in data
        assert data["S001"]["state"] == "IDLE"

    def test_tc_api_05_get_single_printer(self, client):
        """TC-API-05: GET /api/printers/{serial}"""
        resp = client.get("/api/printers/S001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["serial"] == "S001"
        assert data["name"] == "TestP1"

    def test_tc_api_06_camera_404(self, client):
        """TC-API-06: 摄像头无帧时返回404"""
        resp = client.get("/api/printers/S001/camera")
        assert resp.status_code == 404


class TestUsageRoutes:
    def test_tc_api_03_today_usage(self, client):
        """TC-API-03: GET /api/usage/today"""
        resp = client.get("/api/usage/today")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["printer_serial"] == "S001"
        assert data[0]["utilization_percent"] == 4.2

    def test_tc_api_04_monthly_usage(self, client):
        """TC-API-04: GET /api/usage/monthly"""
        resp = client.get("/api/usage/monthly?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["printer_name"] == "TestP1"

    def test_tc_api_07_sessions(self, client):
        """TC-API-07: GET /api/sessions/{serial}"""
        resp = client.get("/api/sessions/S001")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)