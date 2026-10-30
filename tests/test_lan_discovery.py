"""局域网发现模块回归测试 - test_lan_discovery.py

覆盖:
  TC-LD-01: 标准 UDP 通告报文解析出 ip/机器名/型号/状态
  TC-LD-02: 载荷被包在 data 嵌套层时仍能解析
  TC-LD-03: 非 JSON / 缺 ip / ip 非法 / 缺机器名 等各种畸形输入安全降级
  TC-LD-04: mDNS 应答解析出 (服务实例名 -> IP)
  TC-LD-05: 发现缓存按序列号优先、机器名次优查 IP，且大小写与空格容错
  TC-LD-06: 超过 TTL 未再收到通告的设备被自动剔除
  TC-LD-07: enabled=False 时 start() 不监听直接返回 False（默认关闭）
  TC-LD-08: 端口被占用时降级为不可用，不抛异常、不影响服务启动
  TC-LD-09: 探测报文形态（不同固件写法各发一份）
"""

import json
import socket
import struct
import time

import pytest

from bambu_monitor.lan_discovery import (
    DiscoveredPrinter,
    LanDiscovery,
    MDNS_SERVICE,
    _mdns_query,
    build_probe_payload,
    parse_announce,
    parse_mdns_response,
)


def _store(disc: LanDiscovery, **kw) -> DiscoveredPrinter:
    dev = DiscoveredPrinter(**kw)
    disc._store(dev)
    return dev


class TestParseAnnounce:
    def test_tc_ld_01_standard_announce(self):
        """TC-LD-01: 常见通告形态一次解析成功"""
        raw = json.dumps({
            "ip": "192.168.1.23",
            "dev_name": "工作台A",
            "dev_type": "BL-P001",
            "print_status": "RUNNING",
            "interval": 30,
        }).encode()
        dev = parse_announce(raw)
        assert dev is not None
        assert dev.ip == "192.168.1.23"
        assert dev.name == "工作台A"
        assert dev.model == "BL-P001"
        assert dev.print_status == "RUNNING"
        assert dev.source == "udp"

    def test_tc_ld_02_nested_payload(self):
        """TC-LD-02: 有效载荷包在 data 里"""
        raw = json.dumps({
            "command": "get_version",
            "data": {"ip": "10.0.0.7", "name": "P1S", "dev_id": "09876ABCDE"},
        }).encode()
        dev = parse_announce(raw)
        assert dev is not None
        assert dev.ip == "10.0.0.7"
        assert dev.name == "P1S"
        assert dev.dev_id == "09876ABCDE"

    @pytest.mark.parametrize("raw, why", [
        (b"not json at all", "非 JSON"),
        (b"", "空报文"),
        (b"[1,2,3]", "顶层不是对象"),
        (json.dumps({"dev_name": "无 ip 字段"}).encode(), "缺 ip"),
        (json.dumps({"ip": "192.168.1", "dev_name": "残缺地址"}).encode(), "ip 不是四段"),
        (json.dumps({"ip": "", "dev_name": "空 ip"}).encode(), "ip 为空串"),
    ])
    def test_tc_ld_03_malformed_inputs(self, raw, why):
        """TC-LD-03: 畸形输入一律返回 None 而不是抛异常"""
        assert parse_announce(raw) is None, why


def _mdns_packet(instance: str, ip: str) -> bytes:
    """构造一个含 PTR + A 记录的 mDNS 应答，供解析测试使用"""
    def enc(name: str) -> bytes:
        return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"

    qname = enc(MDNS_SERVICE)
    header = struct.pack(">HHHHHH", 0xBABE, 0x8400, 1, 2, 0, 0)
    question = qname + struct.pack(">HH", 12, 1)
    ptr = enc(f"{instance}.{MDNS_SERVICE}") + struct.pack(">HHIH", 12, 1, 120, len(qname)) + qname
    a_rdata = socket.inet_aton(ip)
    a = enc(f"{instance}.{MDNS_SERVICE}") + struct.pack(">HHIH", 1, 1, 120, 4) + a_rdata
    return header + question + ptr + a


class TestParseMdns:
    def test_tc_ld_04_mdns_ptr_and_a(self):
        """TC-LD-04: 从 mDNS 应答中提取实例名与 A 记录"""
        devs = parse_mdns_response(_mdns_packet("x1-carbon", "192.168.1.44"))
        assert len(devs) == 1
        assert devs[0].ip == "192.168.1.44"
        assert devs[0].name == "x1-carbon"
        assert devs[0].source == "mdns"

    @pytest.mark.parametrize("data", [b"", b"\x00\x01", b"garbage" * 20])
    def test_tc_ld_04b_mdns_garbage_is_silent(self, data):
        """TC-LD-04 补充: 无法解析的报文返回空列表，绝不抛异常"""
        assert parse_mdns_response(data) == []


class TestDiscoveryCache:
    def test_tc_ld_05_lookup_by_id_then_name(self):
        """TC-LD-05: 序列号优先命中，机器名次优且大小写/空格容错"""
        disc = LanDiscovery(enabled=True, ttl=90)
        _store(disc, ip="192.168.1.11", dev_id="SERIAL_A", name="Alpha")
        _store(disc, ip="192.168.1.12", dev_id="", name="工作台B")

        assert disc.find_ip(dev_id="SERIAL_A") == "192.168.1.11"
        assert disc.find_ip(name="工作台b") == "192.168.1.12"
        assert disc.find_ip(name="  工作台B  ") == "192.168.1.12"
        assert disc.find_ip(name="不存在") == ""
        assert disc.find_ip() == ""

    def test_tc_ld_06_ttl_eviction(self):
        """TC-LD-06: 超过 TTL 未再通告的设备被剔除"""
        disc = LanDiscovery(enabled=True, ttl=30)
        dev = _store(disc, ip="192.168.1.13", name="Old")
        assert disc.find_ip(name="Old") == "192.168.1.13"

        dev.last_seen = time.time() - 31
        assert disc.find_ip(name="Old") == ""
        assert disc.snapshot() == []

    def test_tc_ld_06b_snapshot_sorted_and_aged(self):
        """snapshot 返回可序列化结构并带 age 字段"""
        disc = LanDiscovery(enabled=True, ttl=90)
        _store(disc, ip="192.168.1.14", name="zzz")
        _store(disc, ip="192.168.1.15", name="aaa")
        snap = disc.snapshot()
        assert [d["name"] for d in snap] == ["aaa", "zzz"]
        assert all("age_seconds" in d for d in snap)

    def test_tc_ld_07_disabled_by_default(self):
        """TC-LD-07: 默认关闭时 start() 不绑定端口"""
        import asyncio
        disc = LanDiscovery()                     # enabled 默认 False
        assert disc.enabled is False
        assert asyncio.run(disc.start()) is False
        assert disc.running is False

    def test_tc_ld_08_bind_failure_degrades(self, monkeypatch):
        """TC-LD-08: 端口绑定失败时降级返回 False，不抛异常

        注：UDP + SO_REUSEADDR 在 Windows 上允许重复绑定，构造真实冲突不可靠，
        因此通过注入 socket 工厂抛 OSError 来覆盖该分支（对应无权限/端口被保留）。
        """
        import asyncio

        import bambu_monitor.lan_discovery as ld

        def _boom():
            raise OSError("permission denied")

        monkeypatch.setattr(ld, "_new_udp_socket", _boom)
        disc = LanDiscovery(enabled=True, port=1880, multicast_dns=False)
        assert asyncio.run(disc.start()) is False
        assert disc.running is False

    def test_tc_ld_09_probe_payloads(self):
        """TC-LD-09: 探测报文覆盖多种固件写法，且带上本机 IP"""
        payloads = build_probe_payload("192.168.1.50")
        assert len(payloads) >= 2
        assert {"command", "data", "ip", "print_status"} <= set(payloads[0])
        assert payloads[0]["ip"] == "192.168.1.50"
        assert payloads[1]["method"] == "get_version"

    def test_mdns_query_is_ptr_request(self):
        """mDNS 查询报文格式自洽：1 个问题、PTR 类型"""
        pkt = _mdns_query(MDNS_SERVICE)
        _tid, _flags, qd, an, ns, ar = struct.unpack(">HHHHHH", pkt[:12])
        assert (qd, an, ns, ar) == (1, 0, 0, 0)
        qtype, qclass = struct.unpack(">HH", pkt[-4:])
        assert (qtype, qclass) == (12, 1)

    def test_ttl_is_clamped(self):
        """ttl 过小会让设备刚发现就被剔除，这里做下限保护"""
        assert LanDiscovery(ttl=1).ttl == 10
