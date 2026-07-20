"""配置加载模块 - 从 YAML 文件加载打印机和全局设置"""

import hashlib
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class CloudConfig(BaseModel):
    """拓竹云端账号配置"""
    username: Optional[str] = None  # 已废弃，保留仅用于 login 子命令
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
    error: bool = Field(default=True, description="HMS 错误时发送通知")


class FeishuSettings(BaseModel):
    """飞书机器人配置"""
    app_id: str = Field(default="", description="飞书应用 App ID")
    app_secret: str = Field(default="", description="飞书应用 App Secret")
    chatid: str = Field(default="", description="群聊 Chat ID")
    enabled: bool = Field(default=False, description="是否启用飞书通知")
    layer3: bool = Field(default=True, description="打印到第3层时发送通知")
    completed: bool = Field(default=True, description="打印完成时发送通知")
    error: bool = Field(default=True, description="HMS 错误时发送通知")


class UiSettings(BaseModel):
    """UI 层面设置"""
    admin_password_hash: str = Field(default="", description="管理员密码 SHA256 哈希")


class AppConfig(BaseModel):
    """应用完整配置"""
    cloud: CloudConfig
    printers: list[PrinterConfig]
    settings: AppSettings = Field(default_factory=AppSettings)
    wecom: WeComSettings = Field(default_factory=WeComSettings)
    feishu: FeishuSettings = Field(default_factory=FeishuSettings)
    ui: UiSettings = Field(default_factory=UiSettings)

    def save_yaml(self, path: str = "printers.yaml"):
        """将当前配置保存到 YAML 文件"""
        data = {
            "cloud": self.cloud.model_dump(),
            "printers": [p.model_dump() for p in self.printers],
            "settings": self.settings.model_dump(),
            "wecom": self.wecom.model_dump(),
            "feishu": self.feishu.model_dump(),
            "ui": self.ui.model_dump(),
        }
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, sort_keys=False)

    @staticmethod
    def hash_password(password: str) -> str:
        """对密码进行 SHA256 哈希"""
        return hashlib.sha256(password.encode()).hexdigest()

    def verify_password(self, password: str) -> bool:
        """验证密码是否正确"""
        if not self.ui.admin_password_hash:
            return False
        return self.ui.admin_password_hash == self.hash_password(password)

    def set_password(self, password: str):
        """设置管理员密码"""
        self.ui.admin_password_hash = self.hash_password(password)


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
    feishu = FeishuSettings(**data.get("feishu", {}))
    ui = UiSettings(**data.get("ui", {}))

    return AppConfig(cloud=cloud, printers=printers, settings=settings, wecom=wecom, feishu=feishu, ui=ui)
