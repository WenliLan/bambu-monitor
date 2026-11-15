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


# ---------------------------------------------------------------------------
# AI 异常检测接口（图库 + 阈值）
# ---------------------------------------------------------------------------

@pytest.fixture
def ai_client():
    """已登录且注入了 mock AI guard 的客户端"""
    app = _create_test_app()
    app.state.config.ui.admin_password_hash = app.state.config.hash_password("admin")
    guard = MagicMock()
    guard.gallery_status.return_value = {
        "available": True, "gallery": {"n_normal": 131, "n_anomaly": 134},
    }
    guard.ingest_gallery = AsyncMock(return_value={"state": "running", "total": 2})
    guard.stage_upload.return_value = "data/normal/a.jpg"
    guard.threshold = 0.7
    guard.set_threshold.return_value = 0.7
    guard.open_alert_folder.return_value = "D:\\proj\\data\\alerts_review"
    app.state._ai_guard = guard
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"password": "admin"}).status_code == 200
    return c, guard


def _upload(c, name="a.jpg", payload=b"\xff\xd8", label="normal"):
    return c.post("/api/ai/gallery/upload", data={"label": label},
                  files=[("files", (name, payload, "image/jpeg"))])


class TestAIGalleryRoutes:
    def test_tc_api_20_gallery_requires_login(self, client):
        """TC-API-20: 三个图库接口未登录一律 401"""
        assert client.get("/api/ai/gallery").status_code == 401
        assert client.post("/api/ai/gallery/ingest").status_code == 401
        assert _upload(client).status_code == 401

    def test_tc_api_21_gallery_without_guard_503(self, auth_client):
        """TC-API-21: AI 未启用时返回 503 而不是 500"""
        assert auth_client.get("/api/ai/gallery").status_code == 503

    def test_tc_api_22_gallery_status(self, ai_client):
        """TC-API-22: 读取图库现状"""
        c, guard = ai_client
        resp = c.get("/api/ai/gallery")
        assert resp.status_code == 200
        assert resp.json()["gallery"]["n_normal"] == 131
        guard.gallery_status.assert_called_once()

    def test_tc_api_23_ingest(self, ai_client):
        """TC-API-23: 启动增量入库"""
        c, guard = ai_client
        resp = c.post("/api/ai/gallery/ingest")
        assert resp.status_code == 200
        assert resp.json()["state"] == "running"
        guard.ingest_gallery.assert_awaited_once()

    def test_tc_api_24_ingest_unavailable_503(self, ai_client):
        """TC-API-24: 检测器不可用时入库返回 503"""
        c, guard = ai_client
        guard.ingest_gallery.side_effect = RuntimeError("AI 检测器不可用")
        resp = c.post("/api/ai/gallery/ingest")
        assert resp.status_code == 503
        assert "不可用" in resp.json()["detail"]

    def test_tc_api_25_upload(self, ai_client):
        """TC-API-25: 上传素材转交 guard 落盘"""
        c, guard = ai_client
        resp = _upload(c)
        assert resp.status_code == 200
        assert resp.json()["saved"] == ["data/normal/a.jpg"]
        guard.stage_upload.assert_called_once_with("normal", "a.jpg", b"\xff\xd8")

    def test_tc_api_26_upload_rejects_bad_file(self, ai_client):
        """TC-API-26: 全部文件非法时返回 400 并带上原因"""
        c, guard = ai_client
        guard.stage_upload.side_effect = ValueError("不支持的图片格式: .txt")
        resp = _upload(c, name="a.txt")
        assert resp.status_code == 400
        assert ".txt" in resp.json()["detail"]


class TestAIThresholdRoute:
    def test_tc_api_27_threshold_requires_login(self, client):
        """TC-API-27: 未登录改阈值一律 401"""
        assert client.post("/api/ai/threshold", json={"threshold": 0.8}).status_code == 401

    def test_tc_api_28_threshold_set(self, ai_client):
        """TC-API-28: 改阈值转交 guard.set_threshold 并回传新旧值"""
        c, guard = ai_client
        guard.set_threshold.return_value = 0.7
        guard.threshold = 0.8
        resp = c.post("/api/ai/threshold", json={"threshold": 0.8})
        assert resp.status_code == 200
        assert resp.json() == {"threshold": 0.8, "previous": 0.7}
        guard.set_threshold.assert_called_once_with(0.8)

    def test_tc_api_29_threshold_range(self, ai_client):
        """TC-API-29: 越界阈值被拒；0 是配置里的"未设置"哨兵，必须挡住"""
        c, guard = ai_client
        for bad in (0.0, -0.1, 0.04, 0.96, 1.0, 2.5):
            resp = c.post("/api/ai/threshold", json={"threshold": bad})
            assert resp.status_code == 422, f"{bad} -> {resp.status_code}"
        guard.set_threshold.assert_not_called()

    def test_tc_api_30_threshold_unavailable_503(self, ai_client):
        """TC-API-30: 检测器不可用时 set_threshold 返回 -1，接口给 503"""
        c, guard = ai_client
        guard.set_threshold.return_value = -1.0
        resp = c.post("/api/ai/threshold", json={"threshold": 0.8})
        assert resp.status_code == 503
        assert "不可用" in resp.json()["detail"]

    def test_tc_api_31_threshold_without_guard_503(self, auth_client):
        """TC-API-31: AI 未启用时返回 503 而不是 500"""
        assert auth_client.post("/api/ai/threshold",
                                json={"threshold": 0.8}).status_code == 503


class TestAIOpenFolderRoute:
    def test_tc_api_32_open_folder_requires_login(self, client):
        """TC-API-32: 未登录不能触发服务端开文件夹"""
        assert client.post("/api/ai/open-folder").status_code == 401

    def test_tc_api_33_open_folder(self, ai_client):
        """TC-API-33: 返回服务端解析出的绝对路径，不接受前端传参"""
        c, guard = ai_client
        resp = c.post("/api/ai/open-folder", json={"path": "C:\\Windows"})
        assert resp.status_code == 200
        assert resp.json() == {"path": "D:\\proj\\data\\alerts_review"}
        guard.open_alert_folder.assert_called_once_with()

    def test_tc_api_34_open_folder_failure_503(self, ai_client):
        """TC-API-34: 平台不支持或系统调用失败都给 503，不是 500"""
        c, guard = ai_client
        for exc in (RuntimeError("仅支持 Windows"), OSError("no association")):
            guard.open_alert_folder.side_effect = exc
            resp = c.post("/api/ai/open-folder")
            assert resp.status_code == 503
            assert "打开文件夹失败" in resp.json()["detail"]

    def test_tc_api_35_open_folder_without_guard_503(self, auth_client):
        """TC-API-35: AI 未启用时返回 503 而不是 500"""
        assert auth_client.post("/api/ai/open-folder").status_code == 503
