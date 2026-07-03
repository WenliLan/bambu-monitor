"""配置加载模块 - 从 YAML 文件加载打印机和全局设置"""

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class CloudConfig(BaseModel):
    """拓竹云端账号配置"""
    username: str = Field(description="拓竹云账号邮箱")
    token_file: str = Field(default="~/.bumbo_token", description="Token 存储路径")
    mqtt_broker: str = Field(default="cn", description="MQTT 服务器区域: cn/us")


class PrinterConfig(BaseModel):
    """单台打印机配置"""
    name: str
    serial: str
    ip: Optional[str] = None             # 仅用于摄像头
    access_code: Optional[str] = None     # 仅用于摄像头
    location: Optional[str] = None


class AppSettings(BaseModel):
    """全局应用设置"""
    camera_interval: int = Field(default=3, description="摄像头帧刷新间隔（秒）")
    db_path: str = Field(default="./data/bumbo_stats.db", description="SQLite 数据库路径")
    host: str = Field(default="0.0.0.0", description="Web 服务监听地址")
    port: int = Field(default=8000, description="Web 服务端口")


class WeComSettings(BaseModel):
    """企业微信机器人配置"""
    bot_id: str = Field(default="", description="智能机器人 Bot ID")
    secret: str = Field(default="", description="智能机器人 Secret")
    chatid: str = Field(default="", description="群聊 chatid，留空则自动发现")
    layer3: bool = Field(default=True, description="打印到第3层时发送通知")
    completed: bool = Field(default=True, description="打印完成时发送通知")


class AppConfig(BaseModel):
    """应用完整配置"""
    cloud: CloudConfig
    printers: list[PrinterConfig]
    settings: AppSettings = Field(default_factory=AppSettings)
    wecom: WeComSettings = Field(default_factory=WeComSettings)


def load_config(config_path: str = "printers.yaml") -> AppConfig:
    """从 YAML 文件加载配置

    Args:
        config_path: YAML 配置文件路径

    Returns:
        AppConfig 实例

    Raises:
        FileNotFoundError: 配置文件不存在
        ValueError: 配置文件格式错误
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data:
        raise ValueError("配置文件为空")

    if "cloud" not in data:
        raise ValueError("配置文件必须包含 'cloud' 字段（拓竹云账号配置）")

    if "printers" not in data:
        raise ValueError("配置文件必须包含 'printers' 字段")

    cloud = CloudConfig(**data["cloud"])
    printers = [PrinterConfig(**p) for p in data["printers"]]
    settings = AppSettings(**data.get("settings", {}))
    wecom = WeComSettings(**data.get("wecom", {}))

    return AppConfig(cloud=cloud, printers=printers, settings=settings, wecom=wecom)
