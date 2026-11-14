from __future__ import annotations
import base64
import hashlib
import hmac
import os
import re
from typing import Any
from omnidesk_agent.config import XConfig
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.channels.channel_auth import secret_equals
from omnidesk_agent.channels.http_client import ChannelHttpClient

# X sends a short random base64-style challenge. The CRC responder is
# unauthenticated and returns HMAC(secret, crc_token), so anything else
# (JSON, whitespace, long strings) is refused rather than signed.
X_CRC_TOKEN_MAX_CHARS = 128
_X_CRC_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_-]{1,%d}" % X_CRC_TOKEN_MAX_CHARS)


def _crc_refusal(status_code: int, detail: str) -> Exception:
    # GET /webhooks/x calls crc_response directly (no WebhookGuard), so a
    # refusal has to carry its own HTTP status instead of becoming a 500.
    from fastapi import HTTPException

    return HTTPException(status_code=status_code, detail=detail)


class XChannel:
    name = "x"
    def extract_envelope(self, payload: dict[str, Any]):
        from omnidesk_agent.channels.base import WebhookEnvelope
        event = (payload.get("direct_message_events") or [{}])[0]
        sender = str((event.get("message_create") or {}).get("sender_id") or "unknown")
        mid = str(event.get("id") or "")
        return WebhookEnvelope(source_key=sender, sender_id=sender, message_id=mid or None, raw=payload)
    def __init__(self, cfg: XConfig):
        self.cfg = cfg
        self.bearer_token = os.getenv(cfg.bearer_token_env, "")
        self.crc_token = os.getenv(cfg.webhook_crc_token_env, "")
        self.http = ChannelHttpClient()

    def crc_response(self, crc_token: str) -> dict[str, str]:
        if not self.cfg.enabled:
            raise _crc_refusal(403, "X channel is disabled")
        if not self.crc_token:
            raise _crc_refusal(503, "X webhook CRC secret is missing")
        if secret_equals(self.crc_token, os.getenv(self.cfg.webhook_secret_env, "")):
            raise _crc_refusal(503, "X webhook CRC secret must differ from the webhook signature secret")
        if not isinstance(crc_token, str) or not _X_CRC_TOKEN_RE.fullmatch(crc_token):
            raise _crc_refusal(400, "invalid crc_token")
        digest = hmac.new(self.crc_token.encode("utf-8"), crc_token.encode("utf-8"), hashlib.sha256).digest()
        return {"response_token": "sha256=" + base64.b64encode(digest).decode("ascii")}


    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload) -> None:
        from omnidesk_agent.channels.verify import env_secret, header, verify_hmac_sha256
        verify_hmac_sha256(body, env_secret(self.cfg.webhook_secret_env, channel=self.name), header(headers, "x-omnidesk-webhook-signature-256"), prefix="sha256=")

    def parse_webhook(self, payload: dict[str, Any]) -> list[ChannelMessage]:
        out = []
        for event in payload.get("direct_message_events", []) or []:
            sender = str(event.get("message_create", {}).get("sender_id", ""))
            if self.cfg.allowed_user_ids and sender not in self.cfg.allowed_user_ids:
                continue
            text = event.get("message_create", {}).get("message_data", {}).get("text", "")
            if text:
                out.append(ChannelMessage(channel=self.name, sender_id=sender, thread_id=sender, message_id=str(event.get("id") or ""), text=text, raw=event))
        return out

    async def post_text(self, text: str, **kwargs) -> None:
        if not self.bearer_token:
            raise RuntimeError("X bearer token is missing")
        return await self.http.post("https://api.x.com/2/tweets", headers={"Authorization": f"Bearer {self.bearer_token}"}, json={"text": text}, idempotency_key=kwargs.get("idempotency_key"), channel=self.name)

    async def send_text(self, recipient: str, text: str, **kwargs) -> None:
        target = str(kwargs.get("target") or recipient).lower()
        if target not in {"public", "timeline", "tweet"}:
            raise NotImplementedError("X send_text only supports explicit public/timeline posting; direct messages are not implemented")
        return await self.post_text(text, idempotency_key=kwargs.get("idempotency_key"))
