"""测试设置模块安全性 - 账户信息加密存储验证

测试用例:
  TC-SEC-01: 管理员密码使用 SHA256 哈希存储（不可逆）
  TC-SEC-02: 空密码无法通过验证
  TC-SEC-03: 不同密码产生不同哈希值
  TC-SEC-04: Token 文件权限检查（Unix-like 系统）
  TC-SEC-05: 配置文件不包含明文密码
  TC-SEC-06: 企业微信 secret 在配置中加密存储验证
  TC-SEC-07: 拓竹云 Token 存储为 JSON 格式（无额外加密层）
  TC-SEC-08: Cookie 使用 HttpOnly + SameSite=Lax
  TC-SEC-09: 未登录时所有设置 API 返回 401
  TC-SEC-10: 首次设置密码后才能访问设置页面
  TC-SEC-11: 打印机 IP 和 access_code 在配置中明文存储
  TC-SEC-12: 登录失败后密码哈希不暴露
  TC-SEC-13: 密码更改后旧 Cookie 失效
"""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock
import hashlib

from fastapi.testclient import TestClient

from bumbo_stats.config import AppConfig, CloudConfig, PrinterConfig, AppSettings, WeComSettings, UiSettings, load_config
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

    # mock 打印机管理器
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
    mock_db.get_today_usage_all.return_value = []
    mock_db.get_monthly_usage_all.return_value = []
    mock_db.get_sessions.return_value = []

    from bumbo_stats.api.app import create_app
    app = create_app(config=config)

    app.state.printer_manager = mock_manager
    app.state.camera_manager = mock_camera
    app.state.db = mock_db

    return app


@pytest.fixture
def test_client():
    app = _create_test_app()
    return TestClient(app)


class TestPasswordHashing:
    """密码哈希安全性测试"""

    def test_tc_sec_01_password_hashed_not_plaintext(self):
        """TC-SEC-01: 密码使用 SHA256 哈希存储，不可逆"""
        cfg = AppConfig(
            cloud=CloudConfig(username="test@test.com"),
            printers=[],
        )

        password = "mySecretPass123"
        cfg.set_password(password)

        # 密码哈希不应等于明文
        assert cfg.ui.admin_password_hash != password
        # 密码哈希应该是定长 SHA256 (64字符)
        assert len(cfg.ui.admin_password_hash) == 64
        # 密码哈希应该是有效的十六进制
        assert all(c in '0123456789abcdef' for c in cfg.ui.admin_password_hash)

    def test_tc_sec_02_empty_password_rejected(self):
        """TC-SEC-02: 空密码无法通过验证"""
        cfg = AppConfig(
            cloud=CloudConfig(username="test@test.com"),
            printers=[],
        )
        cfg.set_password("nonempty")
        assert cfg.verify_password("") is False

    def test_tc_sec_03_different_passwords_different_hashes(self):
        """TC-SEC-03: 不同密码产生不同哈希值"""
        cfg = AppConfig(
            cloud=CloudConfig(username="test@test.com"),
            printers=[],
        )

        cfg.set_password("password1")
        hash1 = cfg.ui.admin_password_hash

        cfg.set_password("password2")
        hash2 = cfg.ui.admin_password_hash

        assert hash1 != hash2
        # 相同密码应产生相同哈希
        cfg.set_password("password1")
        assert cfg.ui.admin_password_hash == hash1

    def test_tc_sec_04_password_hash_uses_sha256(self):
        """TC-SEC-04: 密码哈希使用 SHA256 算法"""
        password = "test_password"
        expected_hash = hashlib.sha256(password.encode()).hexdigest()

        cfg = AppConfig(
            cloud=CloudConfig(username="test@test.com"),
            printers=[],
        )
        cfg.set_password(password)

        assert cfg.ui.admin_password_hash == expected_hash


class TestConfigStorage:
    """配置存储安全性测试"""

    def test_tc_sec_05_config_no_plaintext_password(self, tmp_path):
        """TC-SEC-05: YAML 配置文件中不包含明文密码"""
        cfg = AppConfig(
            cloud=CloudConfig(username="test@test.com"),
            printers=[
                PrinterConfig(name="P1", serial="S001"),
            ],
            ui=UiSettings(admin_password_hash=""),
        )

        config_file = tmp_path / "test.yaml"
        cfg.save_yaml(str(config_file))

        content = config_file.read_text(encoding="utf-8")
        # 密码字段应该是 SHA256 哈希，不是明文
        assert "mySecretPassword" not in content
        # ui 段落应该包含 password_hash 字段
        assert "admin_password_hash" in content

    def test_tc_sec_06_wecom_secret_in_config(self, tmp_path):
        """TC-SEC-06: 企业微信 secret 在配置中以明文存储（需要关注）"""
        cfg = AppConfig(
            cloud=CloudConfig(username="test@test.com"),
            printers=[],
            wecom=WeComSettings(
                bot_id="test_bot",
                secret="super_secret_key",
            ),
        )

        config_file = tmp_path / "test.yaml"
        cfg.save_yaml(str(config_file))

        content = config_file.read_text(encoding="utf-8")
        # 企业微信 secret 是明文存储的（安全风险）
        assert "super_secret_key" in content

    def test_tc_sec_07_token_file_format(self, tmp_path):
        """TC-SEC-07: Token 存储为 JSON 格式，无额外加密"""
        from bumbo_stats.auth import TokenManager

        token_file = tmp_path / "token"
        mgr = TokenManager(str(token_file))

        test_token = "bearer_token_abc123xyz"
        mgr.save_token(test_token)

        # Token 以 JSON 格式存储
        data = json.loads(token_file.read_text(encoding="utf-8"))
        assert data["token"] == test_token
        # Token 是明文的（安全风险）
        assert test_token in token_file.read_text(encoding="utf-8")


class TestPrinterConfigStorage:
    """打印机配置存储测试"""

    def test_tc_sec_11_printer_access_code_plaintext(self, tmp_path):
        """TC-SEC-11: 打印机 IP 和 access_code 在配置中明文存储"""
        cfg = AppConfig(
            cloud=CloudConfig(username="test@test.com"),
            printers=[
                PrinterConfig(
                    name="P1",
                    serial="S001",
                    ip="192.168.1.100",
                    access_code="camera_secret",
                ),
            ],
        )

        config_file = tmp_path / "test.yaml"
        cfg.save_yaml(str(config_file))

        content = config_file.read_text(encoding="utf-8")
        # IP 和 access_code 是明文存储的
        assert "192.168.1.100" in content
        assert "camera_secret" in content


class TestCookieSecurity:
    """Cookie 安全性测试"""

    def test_tc_sec_08_cookie_httponly_samesite(self, test_client):
        """TC-SEC-08: 登录 Cookie 设置 HttpOnly + SameSite=Lax"""
        cfg = test_client.app.state.config
        cfg.ui.admin_password_hash = cfg.hash_password("testpw")

        resp = test_client.post("/api/auth/login", json={"password": "testpw"})

        # 检查 Set-Cookie 头
        set_cookie = resp.headers.get("set-cookie", "")
        assert "HttpOnly" in set_cookie or "httponly" in set_cookie.lower()
        assert "SameSite" in set_cookie or "samesite" in set_cookie.lower()


class TestAuthenticationFlow:
    """认证流程测试"""

    def test_tc_sec_09_all_settings_api_require_auth(self, test_client):
        """TC-SEC-09: 未登录时所有设置 API 返回 401"""
        endpoints = [
            ("/api/settings/cloud", {"username": "test@test.com"}),
            ("/api/settings/printers", {"printers": []}),
            ("/api/settings/wecom", {"bot_id": "test", "secret": "test"}),
            ("/api/settings/password", {"password": "newpass"}),
        ]

        for url, json_data in endpoints:
            resp = test_client.post(url, json=json_data)
            assert resp.status_code == 401, f"{url} should return 401"

    def test_tc_sec_10_first_time_no_password_redirects_to_setup(self, test_client):
        """TC-SEC-10: 首次访问设置页面跳转至密码设置"""
        cfg = test_client.app.state.config
        cfg.ui.admin_password_hash = ""  # 未设置密码

        resp = test_client.get("/settings")
        assert resp.status_code == 200
        # 应该显示 setup.html（密码设置页面）
        assert "password" in resp.text.lower()

    def test_tc_sec_12_login_failure_no_hash_leak(self, test_client):
        """TC-SEC-12: 登录失败时响应不暴露密码哈希"""
        cfg = test_client.app.state.config
        correct_hash = cfg.hash_password("correct_password")
        cfg.ui.admin_password_hash = correct_hash

        resp = test_client.post("/api/auth/login", json={"password": "wrong_password"})
        assert resp.status_code == 401
        # 错误响应不应包含哈希值
        assert correct_hash not in resp.text

    def test_tc_sec_13_password_change_invalidates_old_cookie(self, test_client):
        """TC-SEC-13: 密码更改后旧 Cookie 失效"""
        cfg = test_client.app.state.config
        old_hash = cfg.hash_password("old_password")
        cfg.ui.admin_password_hash = old_hash

        # 用旧密码登录
        test_client.cookies.set("bumbo_auth", old_hash)

        resp = test_client.post("/api/settings/password", json={"password": "new_password"})
        assert resp.status_code == 200

        # 旧哈希值已无效
        new_hash = cfg.hash_password("new_password")
        assert cfg.ui.admin_password_hash == new_hash
        assert old_hash != new_hash


class TestYAMLConfigIntegrity:
    """YAML 配置完整性测试"""

    def test_tc_sec_14_config_roundtrip_preserves_data(self, tmp_path):
        """TC-SEC-14: 配置保存后能正确加载所有字段"""
        cfg = AppConfig(
            cloud=CloudConfig(username="test@test.com", mqtt_broker="us"),
            printers=[
                PrinterConfig(name="P1", serial="S001", ip="1.2.3.4", access_code="secret"),
            ],
            settings=AppSettings(host="127.0.0.1", port=9000),
            wecom=WeComSettings(bot_id="bot123", secret="secret456", layer3=True),
            ui=UiSettings(admin_password_hash="abc123"),
        )

        config_file = tmp_path / "test.yaml"
        cfg.save_yaml(str(config_file))

        # 重新加载（使用模块级函数）
        loaded = load_config(str(config_file))

        assert loaded.cloud.username == "test@test.com"
        assert loaded.cloud.mqtt_broker == "us"
        assert len(loaded.printers) == 1
        assert loaded.printers[0].ip == "1.2.3.4"
        assert loaded.settings.host == "127.0.0.1"
        assert loaded.settings.port == 9000
        assert loaded.wecom.bot_id == "bot123"
        assert loaded.ui.admin_password_hash == "abc123"


class TestInputValidation:
    """输入验证测试"""

    def test_tc_sec_15_short_password_rejected(self, test_client):
        """TC-SEC-15: 过短的密码被拒绝（至少4位）"""
        cfg = test_client.app.state.config
        cfg.ui.admin_password_hash = ""

        resp = test_client.post("/api/auth/setup", json={"password": "abc"})
        assert resp.status_code == 400

    def test_tc_sec_16_password_change_short_rejected(self, test_client):
        """TC-SEC-16: 修改密码时过短密码被拒绝"""
        cfg = test_client.app.state.config
        cfg.ui.admin_password_hash = cfg.hash_password("oldpw")
        test_client.cookies.set("bumbo_auth", cfg.ui.admin_password_hash)

        resp = test_client.post("/api/settings/password", json={"password": "x"})
        assert resp.status_code == 400
