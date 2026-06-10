from __future__ import annotations
import base64, hashlib, hmac, os, time, urllib.parse
from typing import Any, Optional
try:
    import httpx
except ModuleNotFoundError:
    httpx = None
from omnidesk_agent.config import DingTalkConfig
from omnidesk_agent.core.models import ChannelMessage


def _require_httpx():
    if httpx is None:
        raise RuntimeError("httpx is required for outbound channel HTTP calls. Install with: python3 -m pip install httpx")
    return httpx

class DingTalkChannel:
    name = "dingtalk"
    def extract_envelope(self, payload: dict[str, Any]):
        from omnidesk_agent.channels.base import WebhookEnvelope
        sender = str(payload.get("senderStaffId") or payload.get("senderNick") or payload.get("senderId") or "unknown")
        mid = str(payload.get("msgId") or "")
        source = str(payload.get("conversationId") or sender)
        return WebhookEnvelope(source_key=source, sender_id=sender, message_id=mid or None, raw=payload)
    def __init__(self, cfg: DingTalkConfig):
        self.cfg = cfg
        self.robot_token = os.getenv(cfg.robot_access_token_env, "")
        self.robot_secret = os.getenv(cfg.robot_secret_env, "")


    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload) -> None:
        from omnidesk_agent.channels.verify import env_secret, header, verify_hmac_sha256
        verify_hmac_sha256(body, env_secret(self.cfg.webhook_secret_env, channel=self.name), header(headers, "x-omnidesk-webhook-signature-256"), prefix="sha256=")

    def parse_webhook(self, payload: dict[str, Any]) -> Optional[ChannelMessage]:
        conversation_id = str(payload.get("conversationId") or payload.get("conversation_id") or "")
        if self.cfg.allowed_conversation_ids and conversation_id not in self.cfg.allowed_conversation_ids:
            return None
        text_obj = payload.get("text")
        text = text_obj.get("content", "") if isinstance(text_obj, dict) else (payload.get("content") or payload.get("text") or "")
        sender = str(payload.get("senderStaffId") or payload.get("senderId") or payload.get("senderNick") or "unknown")
        if not text:
            return None
        return ChannelMessage(channel=self.name, sender_id=sender, thread_id=conversation_id or sender, message_id=str(payload.get("msgId") or ""), text=str(text).strip(), raw=payload)

    def _signed_webhook_url(self) -> str:
        if not self.robot_token:
            raise RuntimeError("DingTalk robot token is missing")
        url = f"https://oapi.dingtalk.com/robot/send?access_token={self.robot_token}"
        if self.robot_secret:
            timestamp = str(round(time.time() * 1000))
            string_to_sign = f"{timestamp}\n{self.robot_secret}".encode("utf-8")
            digest = hmac.new(self.robot_secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(digest))
            url += f"&timestamp={timestamp}&sign={sign}"
        return url

    async def send_text(self, recipient: str, text: str, **kwargs) -> None:
        body = {"msgtype": "text", "text": {"content": text}}
        async with _require_httpx().AsyncClient(timeout=20) as client:
            r = await client.post(self._signed_webhook_url(), json=body)
            r.raise_for_status()
