"""测试共享配置和 fixtures"""

import sys
from pathlib import Path

import pytest

# 将项目根目录加入 sys.path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ========== Fixtures ==========

@pytest.fixture
def tmp_dir(tmp_path):
    """临时目录"""
    return tmp_path


@pytest.fixture
def sample_config_yaml(tmp_path):
    """创建云端模式示例配置文件"""
    config_file = tmp_path / "printers.yaml"
    db_path = str(tmp_path / "test.db").replace("\\", "/")
    token_path = str(tmp_path / "token").replace("\\", "/")
    config_file.write_text(f"""
cloud:
  username: "test@email.com"
  token_file: "{token_path}"
  mqtt_broker: "cn"

printers:
  - name: "Printer-01"
    serial: "AC12309BH109"
    ip: "192.168.31.5"
    access_code: "12347890"
    location: "工作台A"

  - name: "Printer-02"
    serial: "AC12309BH110"
    ip: "192.168.31.96"
    access_code: "12347891"
    location: "工作台B"

settings:
  camera_interval: 3
  db_path: '{db_path}'
  host: "127.0.0.1"
  port: 8000
""", encoding="utf-8")
    return str(config_file)


@pytest.fixture
def minimal_config_yaml(tmp_path):
    """最小配置（无 settings）"""
    config_file = tmp_path / "minimal.yaml"
    token_path = str(tmp_path / "token").replace("\\", "/")
    config_file.write_text(f"""
cloud:
  username: "test@email.com"
  token_file: "{token_path}"

printers:
  - serial: "TEST001"
    name: "TestPrinter"
""", encoding="utf-8")
    return str(config_file)


@pytest.fixture
def empty_config_yaml(tmp_path):
    """空配置文件"""
    config_file = tmp_path / "empty.yaml"
    config_file.write_text("{}", encoding="utf-8")
    return str(config_file)


@pytest.fixture
def cloud_missing_config_yaml(tmp_path):
    """缺少 cloud 段的配置（测试验证）"""
    config_file = tmp_path / "no_cloud.yaml"
    config_file.write_text("""
printers:
  - serial: "TEST001"
    name: "TestPrinter"
""", encoding="utf-8")
    return str(config_file)


@pytest.fixture
def printers_missing_config_yaml(tmp_path):
    """有 cloud 但缺少 printers 段的配置（测试验证）"""
    config_file = tmp_path / "no_printers.yaml"
    token_path = str(tmp_path / "token").replace("\\", "/")
    config_file.write_text(f"""
cloud:
  username: "test@test.com"
  token_file: "{token_path}"
""", encoding="utf-8")
    return str(config_file)


@pytest.fixture
def invalid_config_yaml(tmp_path):
    """无效配置文件"""
    config_file = tmp_path / "invalid.yaml"
    config_file.write_text("this is not: [valid: yaml: {{", encoding="utf-8")
    return str(config_file)


@pytest.fixture
async def db(tmp_path):
    """初始化临时数据库"""
    from bumbo_stats.database import Database
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.init()
    return database


@pytest.fixture
def printer_names():
    """打印机名称映射"""
    return {
        "AC12309BH109": "Printer-01",
        "AC12309BH110": "Printer-02",
    }
