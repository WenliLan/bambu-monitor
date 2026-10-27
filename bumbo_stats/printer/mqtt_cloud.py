"""拓竹云 MQTT 客户端（自研，基于 paho-mqtt 2.x）

替代 bambu-lab-cloud-api 的 MQTTClient，修复 v0.5.17 部署反馈中的：
- P0-1: client_id 与打印机自身云端会话冲突 → 统一使用随机后缀的
  ``bumbo-monitor-<serial>-<uuid8>``，不再使用 ``bambu-client-`` 前缀；
- P1-2: connect() 立即返回导致后续 pushall 发不出去 → 等待 CONNACK 后
  才向调用方返回成功，失败时抛出带原因的 MQTTError；
- P2-6/P2-7: 不依赖第三方包，回调签名直接对齐 paho v2，避免
  ``_on_disconnect`` 参数数量不匹配导致的异常刷屏。
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import uuid
from typing import Any, Callable, Optional

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - 依赖缺失时由调用方转成可读错误
    mqtt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

MQTT_PORT = 8883
DEFAULT_BROKER = "us.mqtt.bambulab.com"

# 上报 topic 中的 JSON 通常很小；异常大包直接丢弃，避免解析拖垮线程
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024

MessageCallback = Callable[[str, dict], None]


class MQTTError(RuntimeError):
    """MQTT 连接或发送失败"""


def _reason_failed(reason_code: Any) -> bool:
    """兼容 paho v2 ReasonCode 与 int，判断是否为失败"""
    is_failure = getattr(reason_code, "is_failure", None)
    if is_failure is not None:
        return bool(is_failure)
    try:
        return int(reason_code) != 0
    except (TypeError, ValueError):
        return True


class CloudMQTTClient:
    """单台打印机的云端 MQTT 订阅客户端"""

    def __init__(
        self,
        username: str,
        access_token: str,
        device_id: str,
        on_message: Optional[MessageCallback] = None,
        broker: str = DEFAULT_BROKER,
        port: int = MQTT_PORT,
        client_id: Optional[str] = None,
        keepalive: int = 60,
        connect_timeout: float = 10.0,
    ):
        if mqtt is None:
            raise MQTTError(
                "缺少 paho-mqtt 依赖，请执行: pip install \"paho-mqtt>=2.1\""
            )

        self.username = username
        self.access_token = access_token
        self.device_id = device_id
        self.broker = broker
        self.port = port
        self.keepalive = keepalive
        self.connect_timeout = connect_timeout
        self.client_id = client_id or self.make_client_id(device_id)
        self.on_message_callback = on_message

        self.client: Optional[mqtt.Client] = None
        self.message_count = 0
        self.last_data: dict = {}
        self.last_message_at: Optional[float] = None

        self._connected = threading.Event()
        self._closed = False

    @staticmethod
    def make_client_id(device_id: str) -> str:
        """生成全局唯一 client_id（P0-1）

        打印机自己会以固定 client_id 登录云端，监控端必须使用不同且
        每次进程启动都不同的 id，否则云端会踢掉后登录的一方。
        """
        return f"bumbo-monitor-{device_id}-{uuid.uuid4().hex[:8]}"

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def last_message_age(self) -> Optional[float]:
        """距离最后一条消息的秒数；从未收到消息时返回 None"""
        if self.last_message_at is None:
            return None
        return max(0.0, time.monotonic() - self.last_message_at)

    # ---------- paho 回调（paho 2.x 签名） ----------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if _reason_failed(reason_code):
            logger.error(
                "MQTT 连接被拒绝 %s: %s (client_id=%s)",
                self.device_id, reason_code, self.client_id,
            )
            return

        self._connected.set()
        topic = f"device/{self.device_id}/report"
        client.subscribe(topic, qos=0)
        logger.info(
            "MQTT 已连接 %s:%s，订阅 %s (client_id=%s)",
            self.broker, self.port, topic, self.client_id,
        )

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        self._connected.clear()
        if self._closed:
            logger.debug("MQTT 已主动断开 %s", self.device_id)
            return
        logger.warning(
            "MQTT 连接断开 %s: %s（paho 将自动重连）", self.device_id, reason_code
        )

    def _on_message(self, client, userdata, message):
        self.message_count += 1
        self.last_message_at = time.monotonic()

        if len(message.payload) > MAX_PAYLOAD_BYTES:
            logger.warning(
                "MQTT 消息过大，已丢弃 %s: %d 字节",
                self.device_id, len(message.payload),
            )
            return

        try:
            data = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.warning("MQTT 消息解析失败 %s: %s", self.device_id, e)
            return

        if not isinstance(data, dict):
            logger.debug("MQTT 消息不是 JSON 对象，已忽略 %s", self.device_id)
            return

        self.last_data = data
        if self.on_message_callback:
            try:
                self.on_message_callback(self.device_id, data)
            except Exception:
                logger.exception("处理 MQTT 消息回调失败 %s", self.device_id)

    # ---------- 连接管理 ----------

    def connect(self, blocking: bool = False, wait_timeout: Optional[float] = None) -> None:
        """连接云端 broker，并等待 CONNACK

        Args:
            blocking: True 时在当前线程运行网络循环（会阻塞）；一般用 False
            wait_timeout: 等待 CONNACK 的秒数，默认取 connect_timeout

        Raises:
            MQTTError: TCP/TLS 连接失败、认证失败或等待 CONNACK 超时
        """
        self._closed = False
        self._connected.clear()

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.client_id,
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=60)

        mqtt_username = (
            self.username if self.username.startswith("u_") else f"u_{self.username}"
        )
        client.username_pw_set(mqtt_username, self.access_token)

        context = ssl.create_default_context()
        client.tls_set_context(context)

        self.client = client
        logger.info("正在连接 MQTT %s:%s ...", self.broker, self.port)
        try:
            client.connect(self.broker, self.port, keepalive=self.keepalive)
        except (OSError, ValueError) as e:
            self.client = None
            raise MQTTError(f"无法连接 MQTT 服务器 {self.broker}:{self.port}: {e}") from e

        if blocking:
            client.loop_forever()
            return

        client.loop_start()
        timeout = self.connect_timeout if wait_timeout is None else wait_timeout
        if not self._connected.wait(timeout):
            self.disconnect()
            raise MQTTError(
                f"MQTT 连接超时（{timeout:g}s 内未收到 CONNACK）: {self.broker}"
            )

    def wait_connected(self, timeout: Optional[float] = None) -> bool:
        """等待连接建立（用于重连后确认）"""
        timeout = self.connect_timeout if timeout is None else timeout
        return self._connected.wait(timeout)

    def disconnect(self) -> None:
        """主动断开并停止网络循环"""
        self._closed = True
        client = self.client
        self.client = None
        self._connected.clear()
        if client is None:
            return
        try:
            client.disconnect()
        except Exception as e:  # pragma: no cover - 断开失败不影响退出
            logger.debug("MQTT disconnect 异常 %s: %s", self.device_id, e)
        try:
            client.loop_stop()
        except Exception as e:  # pragma: no cover
            logger.debug("MQTT loop_stop 异常 %s: %s", self.device_id, e)
        logger.info("MQTT 已断开: %s", self.device_id)

    # ---------- 指令 ----------

    def publish(self, command: dict) -> None:
        """向打印机下发指令"""
        if self.client is None or not self.connected:
            raise MQTTError(f"MQTT 未连接，无法发送指令: {self.device_id}")
        topic = f"device/{self.device_id}/request"
        info = self.client.publish(topic, json.dumps(command), qos=0)
        if getattr(info, "rc", 0) != 0:
            raise MQTTError(f"MQTT 指令发送失败 rc={info.rc}: {self.device_id}")
        logger.debug("已发送指令 %s: %s", topic, command)

    def request_full_status(self) -> None:
        """请求打印机推送完整状态（pushall）"""
        self.publish({"pushing": {"command": "pushall"}})
        logger.debug("已请求完整状态（pushall）: %s", self.device_id)
