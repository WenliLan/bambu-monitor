"""云端设备导入回归测试 - test_cloud_import.py

覆盖:
  TC-CI-01: 云端原始字段归一化（缺 dev_id 跳过 / 空 name 回落 serial / 字段裁剪）
  TC-CI-02: 未登录云端时候选接口返回 ok=false + no_cloud，而不是 500
  TC-CI-03: 候选列表标注 already_added 与 discovered_ip，且**不下发接入码原文**
  TC-CI-04: 未登录调用导入接口返回 401
  TC-CI-05: 导入时名称/序列号/接入码取自云端，IP 取自局域网发现，并触发热加载
  TC-CI-06: 已添加与云端不存在的序列号被跳过且给出原因
  TC-CI-07: 局域网发现未启用时 IP 留空，其余字段照常导入
  TC-CI-08: 导入写回配置文件的 printers 段（写临时文件，不碰真实 printers.yaml）
"""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from bambu_monitor.cloud_api import BambuCloudClient

from tests.test_api import _create_test_app

# 云端 user/bind 的实测字段形态（值均为合成数据）
RAW_DEVICES = [
    {"dev_id": "S001", "name": "已添加的机器", "dev_model_name": "BL-P001",
     "dev_product_name": "X1", "online": True, "dev_access_code": "11112222",
     "print_status": "IDLE", "total_print_time": 123},
    {"dev_id": "S002", "name": "工作台B", "dev_model_name": "BL-P002",
     "online": True, "dev_access_code": "33334444"},
    {"dev_id": "S003", "name": "", "online": False},          # 无名 -> 回落 serial
    {"name": "没有序列号的脏数据", "online": True},             # 应被跳过
]


def _client(raw=None):
    """构造一个不发网络请求的云端客户端"""
    client = BambuCloudClient(token="t", region="china")
    client.get_devices = lambda: list(RAW_DEVICES if raw is None else raw)
    return client


class FakeLan:
    """局域网发现桩：按名字返回预设 IP"""

    def __init__(self, mapping=None, running=True):
        self.mapping = mapping or {}
        self.running = running

    def find_ip(self, dev_id="", name=""):
        return self.mapping.get(dev_id) or self.mapping.get(name) or ""

    def snapshot(self):
        return [{"ip": ip, "name": name} for name, ip in self.mapping.items()]


@pytest.fixture
def auth_client():
    app = _create_test_app()
    app.state.config.ui.admin_password_hash = app.state.config.hash_password("admin")
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"password": "admin"}).status_code == 200
    return c, app


class TestNormalization:
    def test_tc_ci_01_fields_normalized(self):
        """TC-CI-01: 归一化输出结构稳定，脏数据被剔除"""
        devices = _client().list_importable_devices()
        assert [d["serial"] for d in devices] == ["S001", "S002", "S003"]

        first = devices[0]
        assert first["name"] == "已添加的机器"
        assert first["model"] == "BL-P001"
        assert first["product_name"] == "X1"
        assert first["online"] is True
        assert first["access_code"] == "11112222"
        assert first["print_status"] == "IDLE"

        # 空 name 回落 serial；缺字段回落空串 / False
        assert devices[2]["name"] == "S003"
        assert devices[2]["access_code"] == ""
        assert devices[2]["online"] is False


class TestCandidatesEndpoint:
    def test_tc_ci_02_no_cloud_returns_flag(self, auth_client):
        """TC-CI-02: 未登录云端时给出可提示的错误，而不是异常"""
        client, app = auth_client
        app.state.cloud_client = None
        resp = client.get("/api/settings/cloud/devices")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "no_cloud"
        assert body["devices"] == []

    def test_tc_ci_02b_requires_login(self):
        client = TestClient(_create_test_app())
        assert client.get("/api/settings/cloud/devices").status_code == 401

    def test_tc_ci_03_flags_and_no_secret_leak(self, auth_client):
        """TC-CI-03: 标记已添加与发现 IP，且响应里不含接入码原文"""
        client, app = auth_client
        app.state.cloud_client = _client()
        app.state.lan_discovery = FakeLan({"S002": "192.168.1.77"})

        body = client.get("/api/settings/cloud/devices").json()
        assert body["ok"] is True
        assert body["discovery_enabled"] is True

        by_serial = {d["serial"]: d for d in body["devices"]}
        assert by_serial["S001"]["already_added"] is True      # 配置里已有
        assert by_serial["S002"]["already_added"] is False
        assert by_serial["S002"]["discovered_ip"] == "192.168.1.77"
        assert by_serial["S001"]["has_access_code"] is True
        assert by_serial["S003"]["has_access_code"] is False

        # 关键安全约束：接入码不得出现在下发给浏览器的响应里
        assert "access_code" not in by_serial["S001"]
        assert "11112222" not in client.get("/api/settings/cloud/devices").text


class TestImportEndpoint:
    def test_tc_ci_04_requires_login(self):
        client = TestClient(_create_test_app())
        resp = client.post("/api/settings/printers/import", json={"serials": ["S002"]})
        assert resp.status_code == 401

    def test_tc_ci_04b_no_cloud_is_400(self, auth_client):
        client, app = auth_client
        app.state.cloud_client = None
        assert client.post("/api/settings/printers/import",
                           json={"serials": ["S002"]}).status_code == 400

    def test_tc_ci_05_import_with_ip_and_code(self, auth_client):
        """TC-CI-05: 导入新设备并自动带上云端接入码与局域网发现的 IP"""
        client, app = auth_client
        app.state.cloud_client = _client()
        app.state.lan_discovery = FakeLan({"S002": "192.168.1.77"})
        reload_fn = AsyncMock()
        app.state._reload_printers = reload_fn

        resp = client.post("/api/settings/printers/import", json={"serials": ["S002"]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["added"] == ["S002"]
        assert body["total"] == 2

        imported = [p for p in app.state.config.printers if p.serial == "S002"]
        assert len(imported) == 1
        p = imported[0]
        assert p.name == "工作台B"
        assert p.access_code == "33334444"       # 来自云端，无需手填
        assert p.ip == "192.168.1.77"            # 来自局域网发现
        reload_fn.assert_awaited_once()          # 面板栏位/通知模板随即扩展

    def test_tc_ci_06_skips_with_reasons(self, auth_client):
        """TC-CI-06: 重复与云端不存在的序列号被跳过并说明原因"""
        client, app = auth_client
        app.state.cloud_client = _client()
        app.state.lan_discovery = FakeLan()
        app.state._reload_printers = AsyncMock()

        body = client.post("/api/settings/printers/import",
                           json={"serials": ["S001", "NOPE", "  ", "S002"]}).json()
        assert body["added"] == ["S002"]
        reasons = {s["serial"]: s["reason"] for s in body["skipped"]}
        assert reasons["S001"] == "already_added"
        assert reasons["NOPE"] == "not_in_cloud"
        assert "  " not in reasons               # 空白序列号静默忽略，不进 skipped

    def test_tc_ci_06b_all_duplicates_does_not_reload(self, auth_client):
        client, app = auth_client
        app.state.cloud_client = _client()
        app.state.lan_discovery = FakeLan()
        reload_fn = AsyncMock()
        app.state._reload_printers = reload_fn

        body = client.post("/api/settings/printers/import", json={"serials": ["S001"]}).json()
        assert body["added"] == []
        assert body["total"] == 1
        reload_fn.assert_not_awaited()

    def test_tc_ci_07_no_discovery_leaves_ip_blank(self, auth_client):
        """TC-CI-07: 未启用局域网发现时 IP 留空，其余照常导入"""
        client, app = auth_client
        app.state.cloud_client = _client()
        app.state.lan_discovery = None
        app.state._reload_printers = AsyncMock()

        body = client.post("/api/settings/printers/import", json={"serials": ["S003"]}).json()
        assert body["added"] == ["S003"]
        p = [x for x in app.state.config.printers if x.serial == "S003"][0]
        assert p.ip is None
        assert p.access_code is None
        assert p.name == "S003"

    def test_tc_ci_08_persisted_to_yaml(self, auth_client):
        """TC-CI-08: 导入结果写回配置文件的 printers 段"""
        import yaml

        client, app = auth_client
        app.state.cloud_client = _client()
        app.state.lan_discovery = FakeLan({"S002": "192.168.1.77"})
        app.state._reload_printers = AsyncMock()

        client.post("/api/settings/printers/import", json={"serials": ["S002"]})

        path = app.state.config.config_path
        with open(path, "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        serials = [p["serial"] for p in saved["printers"]]
        assert "S002" in serials
        assert saved["cloud"]["mqtt_broker"] == "cn"   # 其他段保持原样

    def test_cloud_unreachable_is_502(self, auth_client):
        client, app = auth_client

        class Boom(BambuCloudClient):
            def list_importable_devices(self):
                raise RuntimeError("network down")

        app.state.cloud_client = Boom(token="t", region="china")
        assert client.post("/api/settings/printers/import",
                           json={"serials": ["S002"]}).status_code == 502

    def test_candidates_cloud_unreachable_flagged(self, auth_client):
        client, app = auth_client

        class Boom(BambuCloudClient):
            def list_importable_devices(self):
                raise RuntimeError("network down")

        app.state.cloud_client = Boom(token="t", region="china")
        body = client.get("/api/settings/cloud/devices").json()
        assert body["ok"] is False
        assert body["error"] == "cloud_unreachable"
