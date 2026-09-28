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
    dashboard_url: str = Field(
        default="",
        description="通知消息中\"打开监控面板\"的链接（如 http://192.168.1.50:8000/），"
                    "留空则不显示链接",
    )


class WeComSettings(BaseModel):
    """企业微信机器人配置"""
    enabled: bool = Field(
        default=True,
        description="是否启用企业微信通知（总开关）。关闭后所有事件均不入队、不发送",
    )
    bot_id: str = Field(default="", description="智能机器人 Bot ID")
    secret: str = Field(default="", description="智能机器人 Secret")
    chatid: str = Field(default="", description="群聊 chatid，留空则自动发现")
    layer3: bool = Field(default=True, description="打印到第3层时发送通知")
    progress50: bool = Field(default=True, description="打印进度达到50%时发送通知")
    completed: bool = Field(default=True, description="打印完成时发送通知")
    error: bool = Field(default=True, description="HMS 错误时发送通知")


class FeishuSettings(BaseModel):
    """飞书机器人配置"""
    app_id: str = Field(default="", description="飞书应用 App ID")
    app_secret: str = Field(default="", description="飞书应用 App Secret")
    chatid: str = Field(default="", description="群聊 Chat ID")
    enabled: bool = Field(default=False, description="是否启用飞书通知")
    layer3: bool = Field(default=True, description="打印到第3层时发送通知")
    progress50: bool = Field(default=True, description="打印进度达到50%时发送通知")
    completed: bool = Field(default=True, description="打印完成时发送通知")
    error: bool = Field(default=True, description="HMS 错误时发送通知")
    # 应用内加急（urgent_app）：让指定用户的飞书客户端强制弹窗 + 响铃
    urgent_user_id: str = Field(
        default="",
        description="被加急的飞书用户 open_id（多个用英文逗号分隔），配置后该用户在飞书客户端会收到强制弹窗 + 响铃提醒",
    )
    urgent_on_events: list[str] = Field(
        default_factory=lambda: ["error"],
        description="触发应用内加急的事件列表，可选: layer3 / progress50 / completed / error / ai_alarm",
    )
    urgent_max_per_hour: int = Field(
        default=10,
        description="单用户每小时最多加急次数（防止骚扰），0 表示不限制",
    )


class AIGuardSettings(BaseModel):
    """AI 异常报警配置"""
    enabled: bool = Field(default=True, description="是否启用 AI 异常报警")
    interval: int = Field(default=600, description="检测间隔（秒），默认 10 分钟")
    frames_per_check: int = Field(default=10, description="每次检测抓帧数量")
    model: str = Field(default="fff_gray", description="检测模型: dino_ft / fff_gray")
    track: str = Field(default="adapted", description="模型轨道: adapted / zero")
    threshold: float = Field(default=0.0, description="分类阈值，0 表示使用模型推荐默认值")
    alert_frames: int = Field(default=3, description="连续 N 帧异常才触发报警")
    cooldown: int = Field(default=1800, description="报警冷却时间（秒），默认 30 分钟")
    alert_dir: str = Field(default="./data/alerts_review", description="报警图片保存目录")
    deploy_dir: str = Field(
        default="",
        description="AI 检测器代码目录（bambu_ai_deploy_dinoknn）；留空自动探测，"
                    "也可用环境变量 BUMBO_AI_DEPLOY_DIR 覆盖。pip install 部署时必须指定",
    )


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
    ai_guard: AIGuardSettings = Field(default_factory=AIGuardSettings)
    ui: UiSettings = Field(default_factory=UiSettings)
    # 配置文件来源路径（不写入 YAML）；save_yaml 默认写回此路径，
    # 避免测试等场景下误覆盖工作目录中的真实 printers.yaml
    config_path: Optional[str] = Field(default=None, exclude=True)

    def save_yaml(self, path: Optional[str] = None):
        """将当前配置保存到 YAML 文件（默认写回加载来源路径）"""
        path = path or self.config_path or "printers.yaml"
        data = {
            "cloud": self.cloud.model_dump(),
            "printers": [p.model_dump() for p in self.printers],
            "settings": self.settings.model_dump(),
            "wecom": self.wecom.model_dump(),
            "feishu": self.feishu.model_dump(),
            "ai_guard": self.ai_guard.model_dump(),
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
    ai_guard = AIGuardSettings(**data.get("ai_guard", {}))
    ui = UiSettings(**data.get("ui", {}))

    return AppConfig(cloud=cloud, printers=printers, settings=settings, wecom=wecom,
                     feishu=feishu, ai_guard=ai_guard, ui=ui, config_path=str(path))
