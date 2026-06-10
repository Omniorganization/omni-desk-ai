from __future__ import annotations
import os
from typing import Any, Optional
try:
    import httpx
except ModuleNotFoundError:
    httpx = None
from omnidesk_agent.config import LarkConfig, FeishuConfig
from omnidesk_agent.core.models import ChannelMessage


def _require_httpx():
    if httpx is None:
        raise RuntimeError("httpx is required for outbound channel HTTP calls. Install with: python3 -m pip install httpx")
    return httpx

class _BaseLarkFeishuChannel:
    name = "lark"
    def extract_envelope(self, payload: dict[str, Any]):
        from omnidesk_agent.channels.base import WebhookEnvelope
        event = payload.get("event") or {}
        sender = event.get("sender") or {}
        sid = sender.get("sender_id", {}) if isinstance(sender.get("sender_id", {}), dict) else {}
        sender_id = str(sid.get("open_id") or sid.get("user_id") or event.get("open_id") or "unknown")
        message = event.get("message") or {}
        mid = str(message.get("message_id") or payload.get("uuid") or "")
        source = str(message.get("chat_id") or sender_id)
        return WebhookEnvelope(source_key=source, sender_id=sender_id, message_id=mid or None, raw=payload)
    api_base = "https://open.larksuite.com/open-apis"
    def __init__(self, cfg: LarkConfig | FeishuConfig):
        self.cfg = cfg
        self.app_id = os.getenv(cfg.app_id_env, "")
        self.app_secret = os.getenv(cfg.app_secret_env, "")
        self.verification_token = os.getenv(cfg.verification_token_env, "")

    def parse_webhook(self, payload: dict[str, Any]) -> ChannelMessage | Optional[dict[str, str]]:
        if payload.get("type") == "url_verification":
            if self.verification_token and payload.get("token") != self.verification_token:
                return None
            return {"challenge": str(payload.get("challenge", ""))}
        event = payload.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})
        sender_id = (sender.get("sender_id", {}).get("open_id") or sender.get("sender_id", {}).get("user_id") or event.get("open_id") or "unknown")
        if self.cfg.allowed_open_ids and sender_id not in self.cfg.allowed_open_ids:
            return None
        content = message.get("content")
        text = content if isinstance(content, str) else (content.get("text", "") if isinstance(content, dict) else "")
        if not text:
            return None
        return ChannelMessage(channel=self.name, sender_id=str(sender_id), thread_id=str(message.get("chat_id") or sender_id), message_id=str(message.get("message_id") or ""), text=text, raw=payload)

    async def _tenant_access_token(self) -> str:
        if not self.app_id or not self.app_secret:
            raise RuntimeError(f"{self.name} app id/secret missing")
        async with _require_httpx().AsyncClient(timeout=20) as client:
            r = await client.post(f"{self.api_base}/auth/v3/tenant_access_token/internal", json={"app_id": self.app_id, "app_secret": self.app_secret})
            r.raise_for_status()
            data = r.json()
        token = data.get("tenant_access_token")
        if not token:
            raise RuntimeError(f"{self.name} tenant_access_token missing from response")
        return token

    async def send_text(self, recipient: str, text: str, **kwargs) -> None:
        token = await self._tenant_access_token()
        receive_id_type = kwargs.get("receive_id_type", "open_id")
        body = {"receive_id": recipient, "msg_type": "text", "content": {"text": text}}
        async with _require_httpx().AsyncClient(timeout=20) as client:
            r = await client.post(f"{self.api_base}/im/v1/messages?receive_id_type={receive_id_type}", headers={"Authorization": f"Bearer {token}"}, json=body)
            r.raise_for_status()

class LarkChannel(_BaseLarkFeishuChannel):
    name = "lark"
    api_base = "https://open.larksuite.com/open-apis"

class FeishuChannel(_BaseLarkFeishuChannel):
    name = "feishu"
    api_base = "https://open.feishu.cn/open-apis"
