"""Official QQ C2C client. Credentials and API response bodies never enter logs."""

import re

import requests


class DeliveryRejected(RuntimeError):
    """QQ explicitly rejected delivery, so a later retry is allowed."""


class QQBotClient:
    TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
    PRIMARY_API_HOST = "https://api.sgroup.qq.com"
    SANDBOX_API_HOST = "https://sandbox.api.sgroup.qq.com"

    def __init__(self, app_id, client_secret, use_sandbox=False):
        self.app_id = app_id
        self.client_secret = client_secret
        self.use_sandbox = use_sandbox
        self.session = requests.Session()
        self._token = None

    @property
    def host(self):
        return self.SANDBOX_API_HOST if self.use_sandbox else self.PRIMARY_API_HOST

    def get_access_token(self):
        if self._token:
            return self._token
        try:
            response = self.session.post(self.TOKEN_URL, json={
                "appId": self.app_id, "clientSecret": self.client_secret,
            }, timeout=10, allow_redirects=False)
            if response.status_code != 200:
                raise ValueError("authentication failed")
            token = response.json().get("access_token")
            if not isinstance(token, str) or not token:
                raise ValueError("missing token")
        except (requests.RequestException, ValueError, AttributeError):
            raise RuntimeError("QQ 鉴权失败，请检查机器人的 AppID、AppSecret 和权限") from None
        self._token = token
        return token

    def get_gateway_url(self):
        try:
            response = self.session.get(f"{self.host}/gateway", headers={
                "Authorization": f"QQBot {self.get_access_token()}",
            }, timeout=15, allow_redirects=False)
            if response.status_code != 200:
                raise ValueError("gateway failed")
            url = response.json().get("url", "")
            if not isinstance(url, str) or not url.startswith("wss://"):
                raise ValueError("invalid gateway")
        except (requests.RequestException, ValueError, AttributeError):
            raise RuntimeError("QQ 网关获取失败，请检查单聊权限及沙箱配置") from None
        return url

    def send_private_message(self, user_openid, content):
        if not re.fullmatch(r"[0-9a-fA-F]{32}", user_openid):
            raise ValueError("接收人必须为本机器人的 32 位 user_openid，不能使用 QQ 号")
        token = self.get_access_token()
        response = self.session.post(f"{self.host}/v2/users/{user_openid}/messages", json={
            "content": content, "msg_type": 0,
        }, headers={"Authorization": f"QQBot {token}"}, timeout=10, allow_redirects=False)
        if response.status_code in (400, 401, 403, 404, 429):
            # Only expose a numeric error code. The upstream body may contain identifiers.
            code = None
            try:
                code = response.json().get("code")
            except (ValueError, AttributeError):
                pass
            detail = f"，错误码 {code}" if type(code) is int else ""
            raise DeliveryRejected(f"QQ API HTTP {response.status_code}{detail}")
        if response.status_code != 200:
            raise RuntimeError("QQ API 未确认投递成功")
        result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
            raise ValueError("QQ API 未返回消息 ID，投递结果不明")
        return result
