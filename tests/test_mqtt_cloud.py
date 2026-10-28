"""云端 MQTT 客户端回归测试

覆盖 v0.5.17 部署反馈中的两个 P0/P1 缺陷：
  TC-MQTT-01: client_id 唯一且带 bambu-monitor 前缀，不与打印机自身会话冲突
  TC-MQTT-02: connect() 必须等到 CONNACK 才返回，失败/超时抛出 MQTTError
"""

import json
import types

import pytest

from bambu_monitor.printer import mqtt_cloud
from bambu_monitor.printer.mqtt_cloud import CloudMQTTClient, MQTTError


class FakeReasonCode:
    def __init__(self, failure=False):
        self.is_failure = failure


class FakeClient:
    """最小 paho Client 替身，可控制是否回 CONNACK"""

    connack = "ok"  # "ok" / "reject" / None
    instances: list["FakeClient"] = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.credentials = None
        self.subscriptions = []
        self.published = []
        self.connected_to = None
        self.disconnect_calls = 0
        self.loop_stopped = False
        FakeClient.instances.append(self)

    def username_pw_set(self, username, password):
        self.credentials = (username, password)

    def tls_set_context(self, context):
        self.tls_context = context

    def reconnect_delay_set(self, **kwargs):
        pass

    def connect(self, host, port, keepalive=60):
        self.connected_to = (host, port, keepalive)

    def loop_start(self):
        if FakeClient.connack is None:
            return
        self.on_connect(
            self, None, {}, FakeReasonCode(FakeClient.connack == "reject"), None
        )

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.disconnect_calls += 1

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, payload))
        return types.SimpleNamespace(rc=0)


@pytest.fixture
def fake_mqtt(monkeypatch):
    FakeClient.instances = []
    FakeClient.connack = "ok"
    module = types.SimpleNamespace(
        CallbackAPIVersion=types.SimpleNamespace(VERSION2=2),
        Client=FakeClient,
    )
    monkeypatch.setattr(mqtt_cloud, "mqtt", module)
    return FakeClient


def test_tc_mqtt_01_client_id_unique_and_namespaced():
    """TC-MQTT-01: 每次生成都不同，且不与官方客户端前缀冲突"""
    a = CloudMQTTClient.make_client_id("SERIAL123")
    b = CloudMQTTClient.make_client_id("SERIAL123")

    assert a.startswith("bambu-monitor-SERIAL123-")
    assert a != b
    assert len(a.rsplit("-", 1)[1]) == 8
    assert not a.startswith("bambu-client-")


def test_tc_mqtt_01_connect_passes_generated_client_id(fake_mqtt):
    """TC-MQTT-01: connect() 使用自生成 client_id，而非固定值"""
    client = CloudMQTTClient("12345", "tok", "S001", connect_timeout=0.2)
    client.connect(wait_timeout=0.2)

    instance = fake_mqtt.instances[-1]
    assert instance.kwargs["client_id"] == client.client_id
    assert instance.kwargs["client_id"].startswith("bambu-monitor-S001-")
    client.disconnect()


def test_tc_mqtt_02_connect_waits_for_connack(fake_mqtt):
    """TC-MQTT-02: 收到 CONNACK 后才返回，并完成 topic 订阅"""
    client = CloudMQTTClient("12345", "tok", "S001", connect_timeout=0.2)
    client.connect(wait_timeout=0.2)

    assert client.connected is True
    assert fake_mqtt.instances[-1].subscriptions == [("device/S001/report", 0)]
    client.disconnect()
    assert client.connected is False


def test_tc_mqtt_02_connect_times_out_without_connack(fake_mqtt):
    """TC-MQTT-02: 未收到 CONNACK 时不得假装成功，必须抛错并断开"""
    fake_mqtt.connack = None
    client = CloudMQTTClient("12345", "tok", "S001", connect_timeout=0.05)

    with pytest.raises(MQTTError, match="CONNACK"):
        client.connect(wait_timeout=0.05)

    assert client.connected is False
    assert client.client is None
    assert fake_mqtt.instances[-1].disconnect_calls == 1


def test_tc_mqtt_02_rejected_connack_raises(fake_mqtt):
    """TC-MQTT-02: CONNACK 返回失败码时同样视为连接失败"""
    fake_mqtt.connack = "reject"
    client = CloudMQTTClient("12345", "tok", "S001", connect_timeout=0.05)

    with pytest.raises(MQTTError, match="CONNACK"):
        client.connect(wait_timeout=0.05)
    assert client.connected is False


def test_tc_mqtt_03_username_prefix(fake_mqtt):
    """TC-MQTT-03: 云端要求 username 以 u_ 开头，且不重复加前缀"""
    client = CloudMQTTClient("12345", "secret", "S001", connect_timeout=0.2)
    client.connect(wait_timeout=0.2)
    assert fake_mqtt.instances[-1].credentials == ("u_12345", "secret")
    client.disconnect()

    prefixed = CloudMQTTClient("u_abc", "secret", "S001", connect_timeout=0.2)
    prefixed.connect(wait_timeout=0.2)
    assert fake_mqtt.instances[-1].credentials == ("u_abc", "secret")
    prefixed.disconnect()


def test_tc_mqtt_04_pushall_requires_connection(fake_mqtt):
    """TC-MQTT-04: 未连接时下发 pushall 必须给出明确错误"""
    client = CloudMQTTClient("12345", "tok", "S001")
    with pytest.raises(MQTTError, match="未连接"):
        client.request_full_status()


def test_tc_mqtt_04_pushall_after_connect(fake_mqtt):
    """TC-MQTT-04: 连接成功后 pushall 发到 request topic"""
    client = CloudMQTTClient("12345", "tok", "S001", connect_timeout=0.2)
    client.connect(wait_timeout=0.2)
    client.request_full_status()

    topic, payload = fake_mqtt.instances[-1].published[-1]
    assert topic == "device/S001/request"
    assert json.loads(payload) == {"pushing": {"command": "pushall"}}
    client.disconnect()


def test_tc_mqtt_05_message_callback_and_age(fake_mqtt):
    """TC-MQTT-05: 上报消息解析后回调，并刷新 last_message_age"""
    received = []
    client = CloudMQTTClient(
        "12345",
        "tok",
        "S001",
        on_message=lambda device_id, data: received.append((device_id, data)),
        connect_timeout=0.2,
    )
    client.connect(wait_timeout=0.2)

    instance = fake_mqtt.instances[-1]
    message = types.SimpleNamespace(
        payload=json.dumps({"print": {"gcode_state": "RUNNING"}}).encode()
    )
    instance.on_message(instance, None, message)

    assert received == [("S001", {"print": {"gcode_state": "RUNNING"}})]
    assert client.last_message_age() is not None
    assert client.last_message_age() >= 0
    client.disconnect()
