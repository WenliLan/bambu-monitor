"""拓竹云 REST API 客户端（自研，去除 bambu-lab-cloud-api 依赖）

只依赖 `requests`，覆盖监控面板实际用到的三个接口：
- 用户偏好（拿 MQTT 所需的 uid）
- 已绑定设备列表
- 打印任务详情（启动时补录任务名）

设计目标（对应 v0.5.17 部署反馈 P2-5 / P2-9）：
- 不再被第三方包拖入 Flask / OpenCV / flask-limiter 等重型依赖；
- 接口失败时抛出带上下文的 CloudAPIError，便于启动日志定位；
- Token 失效（401/403）单独标记，调用方可降级而不是崩溃。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

CHINA_API = "https://api.bambulab.cn"
GLOBAL_API = "https://api.bambu.com"

# 与登录流程保持一致的客户端标识，避免云端按 UA 做风控
DEFAULT_HEADERS = {
    "User-Agent": "bambu_network_agent/01.09.05.01",
    "X-BBL-Client-Name": "OrcaSlicer",
    "X-BBL-Client-Type": "slicer",
    "X-BBL-Client-Version": "01.09.05.51",
    "X-BBL-Language": "zh-CN",
    "X-BBL-OS-Type": "linux",
    "X-BBL-OS-Version": "6.2.0",
    "X-BBL-Agent-Version": "01.09.05.01",
    "X-BBL-Agent-OS-Type": "linux",
    "accept": "application/json",
    "Content-Type": "application/json",
}


def region_from_broker(mqtt_broker: str | None) -> str:
    """把配置里的 mqtt_broker（cn/us）转换为区域标识（china/global）"""
    value = (mqtt_broker or "").strip().lower()
    return "china" if value in ("cn", "china", "bambulab.cn") else "global"


def broker_for_region(region: str) -> str:
    """区域 -> 云端 MQTT broker 域名"""
    return "cn.mqtt.bambulab.com" if region == "china" else "us.mqtt.bambulab.com"


class CloudAPIError(RuntimeError):
    """拓竹云 REST 接口调用失败"""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_auth_error(self) -> bool:
        return self.status_code in (401, 403)


class BambuCloudClient:
    """拓竹云 REST API 的最小实现"""

    def __init__(
        self,
        token: str,
        region: str = "global",
        timeout: float = 15.0,
        session: Optional[requests.Session] = None,
    ):
        self._token = (token or "").strip()
        self._region = region
        self._timeout = timeout
        self.base_url = CHINA_API if region == "china" else GLOBAL_API
        self._session = session or requests.Session()
        self._session.headers.update(DEFAULT_HEADERS)

    @property
    def token(self) -> str:
        return self._token

    @property
    def region(self) -> str:
        return self._region

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = {"Authorization": f"Bearer {self._token}"}
        timeout = kwargs.pop("timeout", self._timeout)
        try:
            resp = self._session.request(
                method, url, headers=headers, timeout=timeout, **kwargs
            )
        except requests.exceptions.RequestException as e:
            raise CloudAPIError(f"请求云端失败: {method} {endpoint}: {e}") from e

        if resp.status_code >= 400:
            message = ""
            try:
                payload = resp.json()
                if isinstance(payload, dict):
                    message = str(
                        payload.get("message") or payload.get("error") or ""
                    )
            except ValueError:
                message = ""
            if not message:
                message = (resp.text or "").strip()[:200]
            raise CloudAPIError(
                f"云端接口返回 HTTP {resp.status_code}: {endpoint} {message}".strip(),
                status_code=resp.status_code,
            )

        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as e:
            raise CloudAPIError(f"云端接口返回非 JSON: {endpoint}") from e

    def get_user_info(self) -> dict:
        """获取用户偏好信息（含 MQTT 所需的 uid）"""
        data = self._request("GET", "v1/design-user-service/my/preference")
        return data if isinstance(data, dict) else {"uid": data}

    def get_uid(self) -> str:
        """获取 MQTT 用户名需要的 uid；拿不到时抛出 CloudAPIError"""
        info = self.get_user_info()
        uid = info.get("uid", "") if isinstance(info, dict) else info
        uid = str(uid or "").strip()
        if not uid:
            raise CloudAPIError("云端未返回用户 ID（uid），请重新登录")
        return uid

    def get_devices(self) -> list[dict]:
        """获取账号下已绑定的设备列表"""
        data = self._request("GET", "v1/iot-service/api/user/bind")
        if isinstance(data, dict):
            devices = data.get("devices", [])
        elif isinstance(data, list):
            devices = data
        else:
            devices = []
        return [d for d in devices if isinstance(d, dict)]

    def list_importable_devices(self) -> list[dict]:
        """把 ``get_devices`` 的原始字段归一化为「导入」所需的稳定结构

        云端 ``user/bind`` 实测返回（字段随机型/区域可能缺省）::

            dev_id, name, dev_model_name, dev_product_name, dev_structure,
            nozzle_diameter, online, print_job, print_status,
            total_print_time, dev_access_code

        其中 **``dev_access_code`` 就是摄像头接入码**，因此导入无需用户手填；
        云端**不返回局域网 IP**，该字段由局域网发现补全或留空待填。

        Returns:
            每台设备一个 dict：``serial / name / model / product_name / online /
            access_code / print_status``，缺少 ``dev_id`` 的条目会被跳过。
        """
        result: list[dict] = []
        for d in self.get_devices():
            serial = str(d.get("dev_id") or "").strip()
            if not serial:
                continue
            name = str(d.get("name") or "").strip() or serial
            result.append({
                "serial": serial,
                "name": name,
                "model": str(d.get("dev_model_name") or "").strip(),
                "product_name": str(d.get("dev_product_name") or "").strip(),
                "online": bool(d.get("online")),
                "access_code": str(d.get("dev_access_code") or "").strip(),
                "print_status": str(d.get("print_status") or "").strip(),
            })
        return result

    def get_task(self, task_id: str) -> dict:
        """获取打印任务详情"""
        if not task_id:
            return {}
        data = self._request("GET", f"v1/iot-service/api/user/task/{task_id}")
        return data if isinstance(data, dict) else {}

    def validate(self) -> bool:
        """验证 Token 是否可用（不区分网络问题与无效 Token 时返回 False）"""
        try:
            self.get_devices()
            return True
        except CloudAPIError as e:
            logger.debug("Token 验证失败: %s", e)
            return False

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # pragma: no cover - 关闭失败无需处理
            pass
