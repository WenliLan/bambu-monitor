"""测试配置模块 - config.py

测试用例:
  TC-CFG-01: 正常加载云端模式完整配置文件
  TC-CFG-02: 最小配置（无 settings）使用默认值
  TC-CFG-03: 配置文件不存在时抛出 FileNotFoundError
  TC-CFG-04: 缺少 cloud 段抛出 ValueError
  TC-CFG-05: PrinterConfig 必填/选填字段验证
  TC-CFG-06: AppSettings 默认值正确
  TC-CFG-07: CloudConfig 字段验证
"""

import pytest
from bumbo_stats.config import (
    CloudConfig,
    PrinterConfig,
    AppSettings,
    AppConfig,
    load_config,
)


class TestLoadConfig:
    """配置加载测试"""

    def test_tc_cfg_01_load_full_config(self, sample_config_yaml):
        """TC-CFG-01: 正常加载云端模式完整配置文件"""
        config = load_config(sample_config_yaml)

        assert isinstance(config, AppConfig)
        assert isinstance(config.cloud, CloudConfig)
        assert config.cloud.username == "test@email.com"
        assert config.cloud.mqtt_broker == "cn"

        assert len(config.printers) == 2
        assert config.printers[0].name == "Printer-01"
        assert config.printers[0].serial == "AC12309BH109"
        assert config.printers[0].ip == "192.168.31.5"
        assert config.printers[0].access_code == "12347890"
        assert config.printers[1].name == "Printer-02"

        assert config.settings.camera_interval == 3
        assert config.settings.host == "127.0.0.1"
        assert config.settings.port == 8000

    def test_tc_cfg_02_minimal_config(self, minimal_config_yaml):
        """TC-CFG-02: 最小配置使用默认 settings"""
        config = load_config(minimal_config_yaml)

        assert config.cloud.username == "test@email.com"
        assert len(config.printers) == 1
        assert config.printers[0].name == "TestPrinter"
        assert config.printers[0].serial == "TEST001"
        # 默认值
        assert config.settings.camera_interval == 3
        assert config.settings.host == "0.0.0.0"
        assert config.settings.port == 8000

    def test_tc_cfg_03_file_not_found(self):
        """TC-CFG-03: 文件不存在抛出 FileNotFoundError"""
        with pytest.raises(FileNotFoundError, match="配置文件不存在"):
            load_config("nonexistent.yaml")

    def test_tc_cfg_04_cloud_required(self, cloud_missing_config_yaml):
        """TC-CFG-04: 缺少 cloud 段抛出 ValueError"""
        with pytest.raises(ValueError, match="cloud"):
            load_config(cloud_missing_config_yaml)

    def test_tc_cfg_05_printers_required(self, printers_missing_config_yaml):
        """TC-CFG-05: 有 cloud 但缺少 printers 段抛出 ValueError"""
        with pytest.raises(ValueError, match="printers"):
            load_config(printers_missing_config_yaml)


class TestModels:
    """配置模型测试"""

    def test_tc_cfg_06_printer_config_fields(self):
        """TC-CFG-06: PrinterConfig 必填/选填字段"""
        # 必填字段（name + serial）
        pc = PrinterConfig(name="Test", serial="SER001")
        assert pc.name == "Test"
        assert pc.serial == "SER001"
        assert pc.ip is None
        assert pc.access_code is None
        assert pc.location is None

        # 含选填字段
        pc2 = PrinterConfig(
            name="Test2", serial="SER002",
            ip="1.2.3.4", access_code="12345678",
            location="A区",
        )
        assert pc2.ip == "1.2.3.4"
        assert pc2.access_code == "12345678"
        assert pc2.location == "A区"

    def test_tc_cfg_07_app_settings_defaults(self):
        """TC-CFG-07: AppSettings 默认值正确"""
        s = AppSettings()
        assert s.camera_interval == 3
        assert s.host == "0.0.0.0"
        assert s.port == 8000
        assert "./data/" in s.db_path

    def test_tc_cfg_08_cloud_config_defaults(self):
        """TC-CFG-08: CloudConfig 默认值正确"""
        c = CloudConfig(username="test@test.com")
        assert c.token_file == "~/.bumbo_token"
        assert c.mqtt_broker == "cn"


class TestEnvOverrides:
    """环境变量覆盖敏感配置（发布安全: 避免明文凭据落盘）"""

    def test_tc_cfg_09_env_overrides_secrets(self, sample_config_yaml, monkeypatch):
        """TC-CFG-09: 环境变量优先级高于 YAML，覆盖凭据类字段"""
        monkeypatch.setenv("BUMBO_WECOM_SECRET", "env-secret-xyz")
        monkeypatch.setenv("BUMBO_TOKEN_FILE", "/tmp/env_token")
        monkeypatch.setenv("BUMBO_DASHBOARD_URL", "http://env.host:8000/")
        config = load_config(sample_config_yaml)
        assert config.wecom.secret == "env-secret-xyz"
        assert config.cloud.token_file == "/tmp/env_token"
        assert config.settings.dashboard_url == "http://env.host:8000/"

    def test_tc_cfg_10_env_absent_keeps_yaml(self, sample_config_yaml, monkeypatch):
        """TC-CFG-10: 未设置环境变量时保持 YAML 原值"""
        monkeypatch.delenv("BUMBO_WECOM_SECRET", raising=False)
        config = load_config(sample_config_yaml)
        assert config.wecom.secret != "env-secret-xyz"