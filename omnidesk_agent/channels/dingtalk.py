from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from typing import Any, Optional
from omnidesk_agent.config import DingTalkConfig
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.channels.http_client import ChannelHttpClient, ChannelHttpError
from omnidesk_agent.channels.provider_errors import ensure_provider_success

DINGTALK_API_BASE = "https://api.dingtalk.com"
# DingTalk itself allows one hour of skew; the callback is not retried, so a
# tighter window shrinks how long a captured signature can be replayed.
DINGTALK_SIGNATURE_MAX_AGE_SECONDS = 300
# The custom group robot always posts to its own group; this recipient names it.
DINGTALK_ROBOT_RECIPIENT = "robot"


def dingtalk_sign(timestamp: str, secret: str) -> str:
    """base64(HMAC-SHA256(secret, timestamp + "\\n" + secret)), DingTalk's robot signature."""
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    return base64.b64encode(hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()).decode("ascii")


class DingTalkChannel:
    """DingTalk robot adapter.

    Inbound: enterprise-internal robot HTTP callbacks, verified with DingTalk's
    ``timestamp``/``sign`` headers keyed by the app secret. Outbound: replies to
    a group (``group:<openConversationId>`` or a ``cid…`` id) or a user
    (``user:<userId>``) through the robot OpenAPI, or ``robot`` for the custom
    group robot webhook, which can only post to its own group.
    """

    name = "dingtalk"

    def __init__(self, cfg: DingTalkConfig):
        self.cfg = cfg
        self.robot_token = os.getenv(cfg.robot_access_token_env, "")
        self.robot_secret = os.getenv(cfg.robot_secret_env, "")
        self.app_key = os.getenv(cfg.app_key_env, "")
        self.app_secret = os.getenv(cfg.app_secret_env, "")
        robot_code_env = getattr(cfg, "robot_code_env", "")
        self.robot_code = (os.getenv(robot_code_env, "") if robot_code_env else "") or self.app_key
        self._access_token: Optional[tuple[str, float]] = None
        self._seen_signatures: dict[str, float] = {}
        self.http = ChannelHttpClient()

    def extract_envelope(self, payload: dict[str, Any]):
        from omnidesk_agent.channels.base import WebhookEnvelope
        sender = str(payload.get("senderStaffId") or payload.get("senderNick") or payload.get("senderId") or "unknown")
        mid = str(payload.get("msgId") or "")
        source = str(payload.get("conversationId") or sender)
        return WebhookEnvelope(source_key=source, sender_id=sender, message_id=mid or None, raw=payload)

    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload) -> None:
        from omnidesk_agent.channels.verify import env_secret, header, verify_hmac_sha256
        timestamp = header(headers, "timestamp")
        sign = header(headers, "sign")
        if timestamp or sign:
            self._verify_dingtalk_sign(timestamp, sign)
            return
        # Fallback for deployments that put a body-signing bridge in front.
        bridge_secret = os.getenv(self.cfg.webhook_secret_env, "")
        bridge_signature = header(headers, "x-omnidesk-webhook-signature-256")
        if bridge_secret and bridge_signature:
            verify_hmac_sha256(body, env_secret(self.cfg.webhook_secret_env, channel=self.name), bridge_signature, prefix="sha256=")
            return
        raise PermissionError("missing DingTalk timestamp/sign headers")

    def _verify_dingtalk_sign(self, timestamp: str, sign: str) -> None:
        secret = self.app_secret or os.getenv(self.cfg.app_secret_env, "")
        if not secret:
            raise PermissionError(f"dingtalk webhook signature secret is not configured: {self.cfg.app_secret_env}")
        if not timestamp or not sign:
            raise PermissionError("missing DingTalk timestamp/sign headers")
        if not (timestamp.isascii() and timestamp.isdigit() and timestamp == str(int(timestamp))):
            raise PermissionError("invalid DingTalk timestamp")
        now = time.time()
        if abs(now - int(timestamp) / 1000.0) > DINGTALK_SIGNATURE_MAX_AGE_SECONDS:
            raise PermissionError("stale DingTalk webhook signature")
        if not hmac.compare_digest(sign.encode("utf-8", "surrogatepass"), dingtalk_sign(timestamp, secret).encode("ascii")):
            raise PermissionError("invalid DingTalk webhook signature")
        # The signature covers only the timestamp, not the body, so each one is
        # accepted once (per process) within the window.
        window = 2 * DINGTALK_SIGNATURE_MAX_AGE_SECONDS
        self._seen_signatures = {key: seen for key, seen in self._seen_signatures.items() if now - seen <= window}
        if sign in self._seen_signatures:
            raise PermissionError("replayed DingTalk webhook signature")
        self._seen_signatures[sign] = now

    def parse_webhook(self, payload: dict[str, Any]) -> Optional[ChannelMessage]:
        conversation_id = str(payload.get("conversationId") or payload.get("conversation_id") or "")
        if self.cfg.allowed_conversation_ids and conversation_id not in self.cfg.allowed_conversation_ids:
            return None
        text_obj = payload.get("text")
        text = text_obj.get("content", "") if isinstance(text_obj, dict) else (payload.get("content") or payload.get("text") or "")
        sender = str(payload.get("senderStaffId") or payload.get("senderId") or payload.get("senderNick") or "unknown")
        if not text:
            return None
        staff_id = str(payload.get("senderStaffId") or "")
        # A reply to a 1:1 chat goes to the user; to a group, to the conversation.
        thread_id = f"user:{staff_id}" if str(payload.get("conversationType") or "") == "1" and staff_id else (conversation_id or sender)
        return ChannelMessage(channel=self.name, sender_id=sender, thread_id=thread_id, message_id=str(payload.get("msgId") or ""), text=str(text).strip(), raw=payload)

    def _signed_webhook_url(self) -> str:
        if not self.robot_token:
            raise RuntimeError("DingTalk robot token is missing")
        url = f"https://oapi.dingtalk.com/robot/send?access_token={self.robot_token}"
        if self.robot_secret:
            timestamp = str(round(time.time() * 1000))
            url += f"&timestamp={timestamp}&sign={urllib.parse.quote_plus(dingtalk_sign(timestamp, self.robot_secret))}"
        return url

    async def _api_access_token(self) -> str:
        if self._access_token and self._access_token[1] > time.time() + 60:
            return self._access_token[0]
        if not self.app_key or not self.app_secret:
            raise RuntimeError("DingTalk app key/secret missing")
        result = await self.http.post(f"{DINGTALK_API_BASE}/v1.0/oauth2/accessToken", json={"appKey": self.app_key, "appSecret": self.app_secret}, channel=self.name, idempotent=True)
        data = result.data if isinstance(result.data, dict) else {}
        token = data.get("accessToken")
        if not isinstance(token, str) or not token:
            raise ChannelHttpError("DingTalk accessToken missing from response", channel=self.name, request_sent=True, category="config_error")
        try:
            expires_in = int(data.get("expireIn", 7200))
        except (TypeError, ValueError):
            expires_in = 7200
        self._access_token = (token, time.time() + expires_in)
        return token

    async def send_text(self, recipient: str, text: str, **kwargs) -> dict[str, Any]:
        recipient = str(recipient or "")
        idempotency_key = kwargs.get("idempotency_key")
        if recipient == DINGTALK_ROBOT_RECIPIENT:
            result = await self.http.post(self._signed_webhook_url(), json={"msgtype": "text", "text": {"content": text}}, idempotency_key=idempotency_key, channel=self.name)
            ensure_provider_success(self.name, result)
            return {"message_id": None, "request_id": result.request_id}
        kind, _, target = recipient.partition(":")
        if not target and recipient.startswith("cid"):
            kind, target = "group", recipient
        if kind not in {"group", "user"} or not target:
            raise PermissionError("DingTalk recipient must be group:<openConversationId>, user:<userId>, or 'robot' for the custom group robot")
        if not self.robot_code:
            raise RuntimeError("DingTalk robot code is missing (set the app key or robot code)")
        msg_param = json.dumps({"content": text}, ensure_ascii=False)
        headers = {"x-acs-dingtalk-access-token": await self._api_access_token()}
        if kind == "group":
            url = f"{DINGTALK_API_BASE}/v1.0/robot/groupMessages/send"
            body: dict[str, Any] = {"robotCode": self.robot_code, "openConversationId": target, "msgKey": "sampleText", "msgParam": msg_param}
        else:
            url = f"{DINGTALK_API_BASE}/v1.0/robot/oToMessages/batchSend"
            body = {"robotCode": self.robot_code, "userIds": [target], "msgKey": "sampleText", "msgParam": msg_param}
        result = await self.http.post(url, headers=headers, json=body, idempotency_key=idempotency_key, channel=self.name)
        data = result.data if isinstance(result.data, dict) else {}
        if kind == "user":
            if target in (data.get("invalidStaffIdList") or []):
                raise ChannelHttpError("DingTalk rejected the recipient user id", status_code=result.status_code, channel=self.name, request_sent=True, category="bad_request")
            if target in (data.get("flowControlledStaffIdList") or []):
                raise ChannelHttpError("DingTalk flow control held the message for this user", status_code=result.status_code, channel=self.name, request_sent=True, category="rate_limit")
        return {"message_id": data.get("processQueryKey"), "request_id": result.request_id}
