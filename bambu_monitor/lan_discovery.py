"""局域网打印机发现

拓竹打印机在局域网内会以 UDP 广播通告自身（含 IP 与机器名），本模块提供三种
互补的发现手段：

1. **被动监听**：绑定 ``lan_discovery.port``（默认 1880）收通告；
2. **主动探测**：周期性向广播地址发送查询报文，促使设备立即应答；
3. **mDNS 补充**：查询 ``_bambulab._tcp.local``，从应答中解析服务实例名与 A 记录。

用途是在「从云端导入设备」时补全云端接口不返回的 IP 字段。云端 ``user/bind``
已直接给出序列号、机器名与摄像头接入码，因此**发现失败不影响导入本身**，
仅意味着 IP 需要人工补填。

设计约束：
- 任何 socket 绑定/发送失败都只降级为告警，绝不让主服务启动失败；
- 结果带 TTL，设备离线后自动剔除；
- 默认关闭（``lan_discovery.enabled = false``），避免多实例部署抢端口与日志噪声。
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import struct
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
MDNS_SERVICE = "_bambulab._tcp.local"

# 不同固件版本的通告字段名不一致，这里按优先级穷举候选键
_IP_KEYS = ("ip", "dev_ip", "address", "addr")
_NAME_KEYS = ("dev_name", "device_name", "name", "nickname")
_ID_KEYS = ("dev_id", "device_id", "serial", "nn")
_MODEL_KEYS = ("dev_type", "model", "dev_model_name", "type")
_STATUS_KEYS = ("print_status", "state", "status")


@dataclass
class DiscoveredPrinter:
    """一次局域网发现的设备结果"""

    ip: str
    name: str = ""
    dev_id: str = ""
    model: str = ""
    print_status: str = ""
    source: str = "udp"          # udp / mdns
    last_seen: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["age_seconds"] = round(time.time() - self.last_seen, 1)
        return d


def _first_str(obj: dict[str, Any], keys: tuple[str, ...]) -> str:
    """按候选键顺序取第一个非空字符串值"""
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def parse_announce(raw: bytes) -> Optional[DiscoveredPrinter]:
    """解析一条 UDP 通告/应答报文；不是可识别的设备报文时返回 ``None``

    兼容三种形态：顶层即目标对象、包在 ``data`` 里、或包在 ``print_status``
    等嵌套结构里。字段名按 :data:`_IP_KEYS` 等候选列表逐个尝试。
    """
    try:
        text = raw.decode("utf-8", "replace").strip()
    except Exception:  # pragma: no cover - decode 已用 replace, 理论不可达
        return None
    if not text or not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None

    # 某些固件把有效载荷再包一层
    candidates: list[dict[str, Any]] = [obj]
    for key in ("data", "payload", "result"):
        inner = obj.get(key)
        if isinstance(inner, dict):
            candidates.append(inner)

    for cand in candidates:
        ip = _first_str(cand, _IP_KEYS)
        if not ip or ip.count(".") != 3:
            continue
        return DiscoveredPrinter(
            ip=ip,
            name=_first_str(cand, _NAME_KEYS),
            dev_id=_first_str(cand, _ID_KEYS),
            model=_first_str(cand, _MODEL_KEYS),
            print_status=_first_str(cand, _STATUS_KEYS),
            source="udp",
        )
    return None


def _parse_dns_name(data: bytes, offset: int, depth: int = 0) -> tuple[str, int]:
    """解析 DNS 名（含压缩指针），返回 (name, 新的 offset)"""
    labels: list[str] = []
    visited = 0
    while offset < len(data) and visited < 128:
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:  # 压缩指针
            if offset + 1 >= len(data):
                break
            pointer = struct.unpack(">H", data[offset:offset + 2])[0] & 0x3FFF
            offset += 2
            if depth > 4:
                break
            suffix, _ = _parse_dns_name(data, pointer, depth + 1)
            if suffix:
                labels.append(suffix)
            visited += 1
            continue
        chunk = data[offset + 1:offset + 1 + length]
        labels.append(chunk.decode("utf-8", "replace"))
        offset += 1 + length
        visited += 1
    return ".".join(labels), offset


def parse_mdns_response(data: bytes) -> list[DiscoveredPrinter]:
    """从 mDNS 应答中提取 (服务实例名 -> A 记录) 的设备候选

    只关心包含 ``bambulab`` 的 PTR/SRV 记录所在应答；解析不到 IP 时返回空列表，
    不抛异常——mDNS 只是补充手段。
    """
    results: list[DiscoveredPrinter] = []
    try:
        if len(data) < 12:
            return results
        _tid, _flags, qd, an, ns, ar = struct.unpack(">HHHHHH", data[:12])
        offset = 12
        for _ in range(qd):  # 跳过问题段
            _, offset = _parse_dns_name(data, offset)
            offset += 4
        names: list[str] = []
        ips: list[str] = []
        for _ in range(an + ns + ar):
            if offset >= len(data):
                break
            name, offset = _parse_dns_name(data, offset)
            if offset + 10 > len(data):
                break
            rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[offset:offset + 10])
            offset += 10
            rdata = data[offset:offset + rdlen]
            if name.lower().startswith("_bambulab") or "bambulab" in name.lower():
                names.append(name)
            if rtype == 1 and rdlen == 4:  # A 记录
                try:
                    ips.append(socket.inet_ntoa(rdata))
                except OSError:
                    pass
            offset += rdlen
        if ips:
            instance = ""
            for n in names:
                # 形如 "x1-carbon._bambulab._tcp.local" -> "x1-carbon"
                head = n.split("._bambulab", 1)[0]
                if head and not head.startswith("_"):
                    instance = head
                    break
            for ip in ips:
                results.append(DiscoveredPrinter(ip=ip, name=instance, source="mdns"))
    except Exception as e:  # mDNS 解析尽力而为
        logger.debug("mDNS 应答解析失败: %s", e)
    return results


def build_probe_payload(client_ip: str) -> list[dict[str, Any]]:
    """构造促使设备立即应答的探测报文（不同固件接受不同写法，全部发一遍）"""
    return [
        {"command": "get_version", "data": None, "ip": client_ip, "print_status": "IDLE"},
        {"method": "get_version", "ip": client_ip},
    ]


def _new_udp_socket() -> socket.socket:
    """创建发现用的 UDP socket（抽成函数便于测试注入绑定失败）"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    return sock


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    """UDP 协议处理：收到的每个数据报都交给 sink 回调"""

    def __init__(self, sink):
        self._sink = sink
        self.transport = None

    def connection_made(self, transport):  # noqa: ANN001
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        self._sink(data, addr[0])

    def error_received(self, exc: Exception) -> None:
        logger.debug("局域网发现 socket 错误: %s", exc)


class LanDiscovery:
    """局域网打印机发现服务

    用法::

        disc = LanDiscovery(enabled=True, port=1880, ttl=90, probe_interval=15)
        await disc.start()
        disc.snapshot()            # 当前已知设备
        disc.find_ip(name="工作台A")  # 按机器名找 IP
        await disc.stop()
    """

    def __init__(
        self,
        enabled: bool = False,
        port: int = 1880,
        ttl: int = 90,
        probe_interval: int = 15,
        multicast_dns: bool = True,
    ):
        self.enabled = enabled
        self.port = port
        self.ttl = max(10, int(ttl))
        self.probe_interval = int(probe_interval)
        self.multicast_dns = multicast_dns
        self._devices: dict[str, DiscoveredPrinter] = {}
        self._transport = None
        self._mdns_transport = None
        self._tasks: list[asyncio.Task] = []
        self._running = False

    # ---------- 生命周期 ----------

    async def start(self) -> bool:
        """启动监听。失败只告警并返回 False，不抛异常"""
        if not self.enabled:
            logger.info("局域网发现未启用（lan_discovery.enabled=false），跳过")
            return False
        if self._running:
            return True
        loop = asyncio.get_running_loop()
        try:
            sock = _new_udp_socket()
            sock.bind(("0.0.0.0", self.port))
            sock.setblocking(False)
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _DiscoveryProtocol(self._on_packet), sock=sock
            )
        except OSError as e:
            logger.warning("局域网发现端口 %d 绑定失败，功能降级为不可用: %s", self.port, e)
            return False

        self._running = True
        if self.multicast_dns:
            self._start_mdns()
        if self.probe_interval > 0:
            self._tasks.append(asyncio.create_task(self._probe_loop()))
        logger.info(
            "局域网发现已启动: 监听 UDP %d, 探测间隔 %ss, 有效期 %ss, mDNS=%s",
            self.port, self.probe_interval, self.ttl, self.multicast_dns,
        )
        return True

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        for attr in ("_transport", "_mdns_transport"):
            tr = getattr(self, attr, None)
            if tr is not None:
                try:
                    tr.close()
                except Exception:  # pragma: no cover
                    pass
                setattr(self, attr, None)

    # ---------- 收发 ----------

    def _on_packet(self, data: bytes, sender_ip: str) -> None:
        """收到一个 UDP 数据报：优先按设备通告解析，退而尝试 mDNS 应答"""
        dev = parse_announce(data)
        if dev is None:
            for dev in parse_mdns_response(data):
                self._store(dev)
            return
        # 部分固件通告里不带 ip 字段，用来源地址兜底
        if not dev.ip:
            dev.ip = sender_ip
        self._store(dev)

    def _store(self, dev: DiscoveredPrinter) -> None:
        key = dev.dev_id or dev.name or dev.ip
        if not key:
            return
        old = self._devices.get(key)
        if old and (dev.source == "mdns" and old.source == "udp"):
            # mDNS 不携带设备名以外的信息时，不覆盖更可信的 UDP 结果
            dev.name = dev.name or old.name
            dev.dev_id = dev.dev_id or old.dev_id
        dev.ip = dev.ip or (old.ip if old else "")
        self._devices[key] = dev
        if old is None:
            logger.info("局域网发现新设备: %s ip=%s 来源=%s", dev.name or dev.dev_id or "?",
                        dev.ip, dev.source)

    async def _probe_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.probe_interval)
                await self.probe_now()
                self._evict_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:  # 探测失败不应终止循环
                logger.debug("局域网探测异常: %s", e)

    async def probe_now(self) -> int:
        """主动广播一次探测报文，返回成功发出的报文数"""
        if not self._running or self._transport is None:
            return 0
        sent = 0
        client_ip = _local_ip()
        for payload in build_probe_payload(client_ip):
            raw = json.dumps(payload).encode()
            for target in ("255.255.255.255", _subnet_broadcast(client_ip)):
                try:
                    self._transport.sendto(raw, (target, self.port))
                    sent += 1
                except OSError as e:
                    logger.debug("向 %s 发送探测失败: %s", target, e)
        return sent

    def _start_mdns(self) -> None:
        """尽力启动 mDNS：绑定 5353 可能被占用，失败仅降级"""
        loop = asyncio.get_running_loop()

        async def _go():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("", MDNS_PORT))
                except OSError:
                    # 5353 常被系统 mDNS -responder 占用：改为不绑定端口的纯发送
                    sock.close()
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(("0.0.0.0", 0))
                sock.setblocking(False)
                self._mdns_transport, _ = await loop.create_datagram_endpoint(
                    lambda: _DiscoveryProtocol(self._on_packet), sock=sock
                )
                self._mdns_transport.sendto(_mdns_query(MDNS_SERVICE), (MDNS_GROUP, MDNS_PORT))
                logger.debug("mDNS 查询已发出: %s", MDNS_SERVICE)
            except OSError as e:
                logger.debug("mDNS 不可用（降级）: %s", e)

        self._tasks.append(asyncio.create_task(_go()))

    # ---------- 查询 ----------

    def _evict_expired(self) -> None:
        now = time.time()
        for key in [k for k, d in self._devices.items() if now - d.last_seen > self.ttl]:
            self._devices.pop(key, None)

    def snapshot(self) -> list[dict[str, Any]]:
        """返回当前有效的发现结果（已剔除过期项）"""
        self._evict_expired()
        return [d.to_dict() for d in sorted(self._devices.values(), key=lambda x: x.name or x.ip)]

    def find_ip(self, dev_id: str = "", name: str = "") -> str:
        """按序列号优先、机器名次优，找已发现设备的 IP；找不到返回空串"""
        self._evict_expired()
        if dev_id:
            d = self._devices.get(dev_id)
            if d and d.ip:
                return d.ip
        if name:
            target = name.strip().lower()
            for d in self._devices.values():
                if d.name.strip().lower() == target and d.ip:
                    return d.ip
        return ""

    @property
    def running(self) -> bool:
        return self._running


def _local_ip() -> str:
    """取本机用于局域网通信的 IP（无外网时返回 127.0.0.1）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.0.1", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _subnet_broadcast(ip: str) -> str:
    """按 /24 估算子网广播地址（家用网段绝大多数是 /24）"""
    parts = ip.split(".")
    if len(parts) != 4:
        return "255.255.255.255"
    return f"{parts[0]}.{parts[1]}.{parts[2]}.255"


def _mdns_query(service: str) -> bytes:
    """构造一个 PTR 查询报文"""
    tid = 0xBABE  # 查询事务 ID；本机同时只发一条查询，固定值即可
    header = struct.pack(">HHHHHH", tid, 0, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode() for p in service.split(".")) + b"\x00"
    return header + qname + struct.pack(">HH", 12, 1)  # QTYPE=PTR, QCLASS=IN
