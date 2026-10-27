"""Token 认证管理模块 - 拓竹云账号登录与 Token 持久化"""

import json
import logging
import getpass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_FILE = "~/.bumbo_token"

CHINA_API = "https://api.bambulab.cn"
GLOBAL_API = "https://api.bambu.com"

HEADERS = {
    'User-Agent': 'bambu_network_agent/01.09.05.01',
    'X-BBL-Client-Name': 'OrcaSlicer',
    'X-BBL-Client-Type': 'slicer',
    'X-BBL-Client-Version': '01.09.05.51',
    'X-BBL-Language': 'zh-CN',
    'X-BBL-OS-Type': 'linux',
    'X-BBL-OS-Version': '6.2.0',
    'X-BBL-Agent-Version': '01.09.05.01',
    'X-BBL-Agent-OS-Type': 'linux',
    'accept': 'application/json',
    'Content-Type': 'application/json'
}


class TokenManager:
    """管理拓竹云端 API 的 Token 存取和登录流程"""

    def __init__(self, token_file: str = DEFAULT_TOKEN_FILE):
        self._token_path = Path(token_file).expanduser()

    @property
    def token_path(self) -> Path:
        return self._token_path

    def load_token(self) -> Optional[str]:
        """从文件加载已保存的 Token"""
        try:
            if not self._token_path.exists():
                logger.debug("Token 文件不存在: %s", self._token_path)
                return None

            text = self._token_path.read_text(encoding="utf-8").strip()
            if not text:
                logger.debug("Token 文件为空: %s", self._token_path)
                return None

            try:
                data = json.loads(text)
                token = data.get("token") if isinstance(data, dict) else data
            except json.JSONDecodeError:
                token = text

            # 确保 token 没有多余的空白字符
            if isinstance(token, str):
                token = token.strip()

            if not token:
                return None

            logger.info("已加载 Token: %s", self._token_path)
            return token
        except Exception as e:
            logger.warning("读取 Token 文件失败: %s", e)
            return None

    def save_token(self, token: str, region: str = "global") -> None:
        """将 Token 保存到文件"""
        try:
            self._token_path.parent.mkdir(parents=True, exist_ok=True)
            data = {"region": region, "token": token}
            self._token_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            logger.info("Token 已保存到: %s", self._token_path)
        except Exception as e:
            logger.error("保存 Token 失败: %s", e)
            raise

    def _get_base_url(self, region: str) -> str:
        return CHINA_API if region == "china" else GLOBAL_API

    def interactive_login(self, username: str, password: str, region: str = "global") -> str:
        """交互式登录拓竹云账号（含验证码两步验证）

        Args:
            username: 拓竹云账号（手机号或邮箱）
            password: 密码
            region: API 区域，"global" 或 "china"

        Returns:
            登录成功后返回 access token
        """
        import requests

        base_url = self._get_base_url(region)
        session = requests.Session()
        session.headers.update(HEADERS)

        # Step 1: 提交账号+密码
        login_payload = {"account": username, "password": password, "apiError": ""}

        try:
            resp = session.post(
                f"{base_url}/v1/user-service/user/login",
                json=login_payload,
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"登录请求失败: {e}") from e

        if data.get("success"):
            token = data.get("accessToken")
            if token:
                self.save_token(token, region=region)
                logger.info("登录成功（无需验证码）")
                return token
            raise RuntimeError("登录成功但未返回 Token")

        login_type = data.get("loginType")

        if login_type == "verifyCode":
            # 中国区：手机号+密码 → 短信验证码
            # 全球区：邮箱+密码 → 邮件验证码
            return self._handle_verify_code(session, base_url, username, region)

        elif login_type == "tfa":
            tfa_key = data.get("tfaKey")
            return self._handle_mfa(session, base_url, tfa_key, region)

        else:
            error_msg = data.get("message", data.get("error", "未知登录错误"))
            raise RuntimeError(f"登录失败: {error_msg}")

    def _handle_verify_code(self, session, base_url: str, username: str, region: str) -> str:
        """处理验证码验证（中国区用短信，全球区用邮件）"""
        hint = self._send_verify_code(session, base_url, username, region)
        logger.info(hint)
        print(hint)

        # 交互式获取验证码（getpass 不回显，避免误把验证码留在终端）
        code = getpass.getpass("请输入验证码，输入后按回车: ")
        if not code.strip():
            raise RuntimeError("验证码不能为空")
        return self._verify_code_token(session, base_url, username, region, code.strip())

    def _send_verify_code(self, session, base_url: str, username: str, region: str) -> str:
        """发送短信/邮件验证码并校验接口响应（P3-8：不能只看 HTTP 200）"""
        if region == "china":
            send_payload = {"phone": username, "countryCode": "86"}
            endpoint = f"{base_url}/v1/user-service/user/sendsmscode"
            hint = "短信验证码已发送到你的手机"
        else:
            send_payload = {"email": username, "type": "codeLogin"}
            endpoint = f"{base_url}/v1/user-service/user/sendemail/code"
            hint = "验证码已发送到你的注册邮箱"

        resp = session.post(endpoint, json=send_payload, timeout=30)
        try:
            resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"验证码发送失败: HTTP {resp.status_code}") from e
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("success") is False:
            reason = data.get("message") or data.get("error") or "接口返回失败"
            raise RuntimeError(f"验证码发送失败: {reason}")
        return hint

    def _verify_code_token(self, session, base_url: str, username: str, region: str, code: str) -> str:
        """用验证码换取 Token 并落盘"""
        verify_payload = {"account": username, "code": code}
        resp = session.post(
            f"{base_url}/v1/user-service/user/login",
            json=verify_payload,
            timeout=30
        )
        try:
            resp.raise_for_status()
        except Exception:
            try:
                err_data = resp.json()
                api_error = err_data.get("error", "")
                if api_error:
                    raise RuntimeError(f"验证码错误: {api_error}")
            except Exception:
                pass
            raise RuntimeError(f"验证码验证失败: HTTP {resp.status_code}")
        verify_data = resp.json()

        token = verify_data.get("accessToken")
        if not token:
            error_msg = verify_data.get("message", verify_data.get("error", "验证码验证失败"))
            raise RuntimeError(f"验证码验证失败: {error_msg}")

        self.save_token(token, region=region)
        logger.info("登录成功")
        return token

    def begin_login(self, username: str, password: str, region: str = "global") -> dict:
        """非交互式登录第一步（供网页端调用）

        Returns:
            {"status": "ok", "token": str}
            或 {"status": "need_code", "session": requests.Session,
                "base_url": str, "username": str, "region": str, "hint": str}
            或 {"status": "need_tfa", "session": ..., "base_url": str,
                "tfa_key": str, "username": str, "region": str}
        """
        import requests

        base_url = self._get_base_url(region)
        session = requests.Session()
        session.headers.update(HEADERS)

        login_payload = {"account": username, "password": password, "apiError": ""}
        try:
            resp = session.post(
                f"{base_url}/v1/user-service/user/login",
                json=login_payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"登录请求失败: {e}") from e

        if data.get("success"):
            token = data.get("accessToken")
            if not token:
                raise RuntimeError("登录成功但未返回 Token")
            self.save_token(token, region=region)
            return {"status": "ok", "token": token}

        login_type = data.get("loginType")
        if login_type == "verifyCode":
            hint = self._send_verify_code(session, base_url, username, region)
            return {
                "status": "need_code", "session": session, "base_url": base_url,
                "username": username, "region": region, "hint": hint,
            }
        if login_type == "tfa":
            return {
                "status": "need_tfa", "session": session, "base_url": base_url,
                "tfa_key": data.get("tfaKey", ""), "username": username, "region": region,
            }

        error_msg = data.get("message", data.get("error", "未知登录错误"))
        raise RuntimeError(f"登录失败: {error_msg}")

    def complete_login(self, pending: dict, code: str) -> str:
        """非交互式登录第二步：提交短信/邮件验证码或 MFA 码"""
        if pending.get("status") == "need_code":
            return self._verify_code_token(
                pending["session"], pending["base_url"],
                pending["username"], pending["region"], code,
            )
        if pending.get("status") == "need_tfa":
            resp = pending["session"].post(
                f"{pending['base_url']}/api/sign-in/tfa",
                json={"tfaKey": pending.get("tfa_key", ""), "tfaCode": code},
                timeout=30,
            )
            resp.raise_for_status()
            token = resp.cookies.get_dict().get("token")
            if not token:
                data = resp.json()
                token = data.get("accessToken") or data.get("token")
            if not token:
                raise RuntimeError("MFA 验证失败")
            self.save_token(token, region=pending["region"])
            return token
        raise RuntimeError("没有待完成的登录流程")

    def _handle_mfa(self, session, base_url: str, tfa_key: str, region: str) -> str:
        """处理 MFA 两步验证"""
        print("多因素认证已启用，请输入 MFA 验证码: ", end="", flush=True)
        code = getpass.getpass("")
        if not code.strip():
            raise RuntimeError("MFA 验证码不能为空")

        verify_payload = {"tfaKey": tfa_key, "tfaCode": code.strip()}
        resp = session.post(
            f"{base_url}/api/sign-in/tfa",
            json=verify_payload,
            timeout=30
        )
        resp.raise_for_status()

        cookies = resp.cookies.get_dict()
        token = cookies.get("token")
        if not token:
            data = resp.json()
            token = data.get("accessToken") or data.get("token")

        if not token:
            raise RuntimeError("MFA 验证失败")

        self.save_token(token, region=region)
        logger.info("登录成功（MFA）")
        return token

    def get_or_login(self, username: str, password: str, region: str = "global") -> str:
        """尝试加载已有 Token，失效则重新登录"""
        token = self.load_token()
        if token:
            if self._validate_token(token, region=region):
                return token
            logger.info("已有 Token 失效，需要重新登录")

        return self.interactive_login(username, password, region=region)

    def _validate_token(self, token: str, region: str = "global") -> bool:
        """验证 Token 是否仍然有效"""
        from .cloud_api import BambuCloudClient, CloudAPIError

        client = BambuCloudClient(token=token, region=region)
        try:
            client.get_devices()
            return True
        except CloudAPIError as e:
            logger.debug("Token 验证失败: %s", e)
            return False
        finally:
            client.close()