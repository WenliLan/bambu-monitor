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
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from datetime import date

from fastapi.testclient import TestClient

from bambu_monitor.config import AppConfig, PrinterConfig, AppSettings, CloudConfig
from bambu_monitor.models import PrinterStatus, PrinterState, TodayUsageItem, MonthlyUsage, MonthlyUsagePoint


def _create_test_app():
    """创建一个带 mock 的测试应用"""
    config = AppConfig(
        cloud=CloudConfig(username="test@test.com"),
        printers=[
            PrinterConfig(name="TestP1", serial="S001", ip="1.2.3.4", access_code="0000"),
        ],
        settings=AppSettings(db_path=":memory:"),
        # 重定向保存路径到临时目录，防止测试覆盖真实 printers.yaml
        config_path=str(Path(tempfile.mkdtemp()) / "printers.yaml"),
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

    from bambu_monitor.api.app import create_app
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


@pytest.fixture
def auth_client():
    """已登录客户端（P2-14 之后所有页面/API 都需要会话）"""
    app = _create_test_app()
    app.state.config.ui.admin_password_hash = app.state.config.hash_password("admin")
    c = TestClient(app)
    resp = c.post("/api/auth/login", json={"password": "admin"})
    assert resp.status_code == 200
    return c


class TestDashboardRoute:
    def test_tc_api_01_dashboard_html(self, auth_client):
        """TC-API-01: GET / 返回 HTML 页面"""
        resp = auth_client.get("/")
        assert resp.status_code == 200
        assert "Bambu Monitor" in resp.text
        assert "camera-grid" in resp.text


class TestPrinterRoutes:
    def test_tc_api_02_get_all_printers(self, auth_client):
        """TC-API-02: GET /api/printers"""
        resp = auth_client.get("/api/printers")
        assert resp.status_code == 200
        data = resp.json()
        assert "S001" in data
        assert data["S001"]["state"] == "IDLE"

    def test_tc_api_05_get_single_printer(self, auth_client):
        """TC-API-05: GET /api/printers/{serial}"""
        resp = auth_client.get("/api/printers/S001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["serial"] == "S001"
        assert data["name"] == "TestP1"

    def test_tc_api_06_camera_404(self, auth_client):
        """TC-API-06: 摄像头无帧时返回404"""
        resp = auth_client.get("/api/printers/S001/camera")
        assert resp.status_code == 404


class TestUsageRoutes:
    def test_tc_api_03_today_usage(self, auth_client):
        """TC-API-03: GET /api/usage/today"""
        resp = auth_client.get("/api/usage/today")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["printer_serial"] == "S001"
        assert data[0]["utilization_percent"] == 4.2

    def test_tc_api_04_monthly_usage(self, auth_client):
        """TC-API-04: GET /api/usage/monthly"""
        resp = auth_client.get("/api/usage/monthly?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["printer_name"] == "TestP1"

    def test_tc_api_07_sessions(self, auth_client):
        """TC-API-07: GET /api/sessions/{serial}"""
        resp = auth_client.get("/api/sessions/S001")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


class TestSettingsRoutes:
    """设置页面和 API 测试"""
    AUTH_COOKIE = "bambu_monitor_auth"

    def _set_auth(self, client, password="admin"):
        cfg = client.app.state.config
        cfg.ui.admin_password_hash = cfg.hash_password(password)
        resp = client.post("/api/auth/login", json={"password": password})
        assert resp.status_code == 200

    def test_tc_api_08_settings_page_requires_auth(self, client):
        """TC-API-08: 未登录访问 /settings 显示登录页"""
        resp = client.get("/settings")
        assert resp.status_code == 200
        # 未登录时应显示登录表单（页面包含 password 输入框）
        assert "password" in resp.text.lower()

    def test_tc_api_09_login_wrong_password(self, client):
        """TC-API-09: 登录密码错误返回 401"""
        cfg = client.app.state.config
        cfg.ui.admin_password_hash = cfg.hash_password("right")
        resp = client.post("/api/auth/login", json={"password": "wrong"})
        assert resp.status_code == 401

    def test_tc_api_10_login_correct_password(self, client):
        """TC-API-10: 正确密码登录设置 cookie"""
        cfg = client.app.state.config
        cfg.ui.admin_password_hash = cfg.hash_password("testpw")
        resp = client.post("/api/auth/login", json={"password": "testpw"})
        assert resp.status_code == 200
        assert self.AUTH_COOKIE in resp.cookies

    def test_tc_api_11_settings_page_with_auth(self, client):
        """TC-API-11: 登录后访问设置页面显示配置"""
        cfg = client.app.state.config
        cfg.ui.admin_password_hash = cfg.hash_password("testpw")
        client.post("/api/auth/login", json={"password": "testpw"})
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert "Token 文件路径" in resp.text

    def test_tc_api_12_save_cloud_config(self, client):
        """TC-API-12: 保存云端配置"""
        cfg = client.app.state.config
        self._set_auth(client, "admin")
        resp = client.post("/api/settings/cloud", json={
            "token_file": "~/.bambu_monitor_token",
            "mqtt_broker": "us",
        })
        assert resp.status_code == 200
        assert cfg.cloud.mqtt_broker == "us"

    def test_tc_api_13_save_printers_config(self, client):
        """TC-API-13: 保存打印机配置"""
        cfg = client.app.state.config
        self._set_auth(client, "admin")
        cfg._reload_printers = AsyncMock()
        resp = client.post("/api/settings/printers", json={
            "printers": [
                {"name": "P1", "serial": "S002", "ip": "1.2.3.4",
                 "access_code": "0000", "location": "客厅"},
            ],
        })
        assert resp.status_code == 200
        assert len(cfg.printers) == 1
        assert cfg.printers[0].serial == "S002"

    def test_tc_api_14_save_wecom_config(self, client):
        """TC-API-14: 保存企业微信配置"""
        cfg = client.app.state.config
        self._set_auth(client, "admin")
        cfg._reload_wecom = AsyncMock()
        resp = client.post("/api/settings/wecom", json={
            "bot_id": "bot123",
            "secret": "secret456",
            "chatid": "chat789",
            "layer3": True,
            "progress50": False,
            "completed": True,
            "error": False,
        })
        assert resp.status_code == 200
        assert cfg.wecom.bot_id == "bot123"
        assert cfg.wecom.progress50 is False
        assert cfg.wecom.error is False

    def test_tc_api_15_change_password(self, client):
        """TC-API-15: 修改管理员密码"""
        cfg = client.app.state.config
        self._set_auth(client, "oldpw")
        resp = client.post("/api/settings/password", json={"password": "newpw"})
        assert resp.status_code == 200
        assert cfg.ui.admin_password_hash == cfg.hash_password("newpw")

    def test_tc_api_16_logout(self, client):
        """TC-API-16: 退出登录删除 cookie"""
        resp = client.post("/api/auth/logout")
        assert resp.status_code == 200

    def test_tc_api_17_unauthenticated_api_returns_401(self, client):
        """TC-API-17: 未登录调用 settings API 返回 401"""
        # 使用有效数据，但不带认证 cookie
        test_cases = [
            ("/api/settings/cloud", {"username": "test@test.com"}),
            ("/api/settings/printers", {"printers": []}),
            ("/api/settings/wecom", {"bot_id": "test", "secret": "test"}),
            ("/api/settings/password", {"password": "newpass"}),
        ]
        for url, json_data in test_cases:
            resp = client.post(url, json=json_data)
            assert resp.status_code == 401, f"{url} returned {resp.status_code}"

    def test_tc_api_18_setup_first_password(self, client):
        """TC-API-18: 首次设置密码"""
        cfg = client.app.state.config
        cfg.ui.admin_password_hash = ""  # 未设置过密码
        resp = client.post("/api/auth/setup", json={"password": "newadmin"})
        assert resp.status_code == 200
        assert cfg.ui.admin_password_hash == cfg.hash_password("newadmin")

    def test_tc_api_19_printers_reload_called(self, client):
        """TC-API-19: 保存打印机后调用重载函数"""
        self._set_auth(client, "admin")
        # _reload_printers 应该在 app.state 上（与 routes.py 中一致）
        reload_mock = AsyncMock()
        client.app.state._reload_printers = reload_mock
        resp = client.post("/api/settings/printers", json={
            "printers": [{"name": "P1", "serial": "X001", "ip": "", "access_code": "", "location": ""}],
        })
        assert resp.status_code == 200
        reload_mock.assert_called_once()
