from __future__ import annotations
import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from typing import Any, Mapping, Optional
from urllib.parse import quote, urlsplit, parse_qsl
from omnidesk_agent.config import XConfig
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.channels.channel_auth import secret_equals
from omnidesk_agent.channels.http_client import ChannelHttpClient

X_API_BASE = "https://api.x.com"
# X sends a short random base64-style challenge. The CRC responder is
# unauthenticated and returns HMAC(secret, crc_token), so anything else
# (JSON, whitespace, long strings) is refused rather than signed. Event bodies
# are JSON objects, so a CRC response can never double as an event signature.
X_CRC_TOKEN_MAX_CHARS = 128
_X_CRC_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_-]{1,%d}" % X_CRC_TOKEN_MAX_CHARS)
_X_USER_ID_RE = re.compile(r"[0-9]{1,25}")


def _crc_refusal(status_code: int, detail: str) -> Exception:
    # GET /webhooks/x calls crc_response directly (no WebhookGuard), so a
    # refusal has to carry its own HTTP status instead of becoming a 500.
    from fastapi import HTTPException

    return HTTPException(status_code=status_code, detail=detail)


def x_signature(consumer_secret: str, data: bytes) -> str:
    """``sha256=`` + base64 HMAC-SHA256, the format of X's CRC response and event signature."""
    return "sha256=" + base64.b64encode(hmac.new(consumer_secret.encode("utf-8"), data, hashlib.sha256).digest()).decode("ascii")


def _pct(value: str) -> str:
    return quote(str(value), safe="~")


def oauth1_authorization(
    method: str,
    url: str,
    *,
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
    extra_params: Optional[Mapping[str, str]] = None,
    nonce: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> str:
    """OAuth 1.0a (RFC 5849) HMAC-SHA1 ``Authorization`` header for a user-context X request.

    A JSON body is not part of the signature base string; query parameters and
    form parameters (``extra_params``) are.
    """
    parts = urlsplit(url)
    base_url = f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}"
    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    pairs = list(parse_qsl(parts.query, keep_blank_values=True)) + list((extra_params or {}).items()) + list(oauth_params.items())
    normalized = "&".join(f"{k}={v}" for k, v in sorted((_pct(k), _pct(v)) for k, v in pairs))
    base_string = "&".join([method.upper(), _pct(base_url), _pct(normalized)])
    signing_key = f"{_pct(consumer_secret)}&{_pct(token_secret)}"
    # HMAC-SHA1 is the signature method OAuth 1.0a defines (RFC 5849 §3.4.2) and
    # the only one X accepts for user-context requests. The secrets are an HMAC
    # key for signing this request, not a password being hashed for storage.
    signature = base64.b64encode(hmac.new(signing_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1).digest()).decode("ascii")
    oauth_params["oauth_signature"] = signature
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth_params.items()))


class XChannel:
    """X (Twitter) adapter: Account Activity API webhooks and v2 posting.

    Inbound events carry ``X-Twitter-Webhooks-Signature-OAuth2`` (keyed by the
    OAuth 2.0 client secret) and/or the legacy ``X-Twitter-Webhooks-Signature``
    (keyed by the OAuth 1.0a consumer secret): base64 HMAC-SHA256 of the body,
    the same scheme as the CRC response. ``X_WEBHOOK_CRC_TOKEN`` holds whichever
    secret the app's webhook uses. Outbound posts and DMs use OAuth 1.0a user
    context.
    """

    name = "x"

    def __init__(self, cfg: XConfig):
        self.cfg = cfg
        self.bearer_token = os.getenv(cfg.bearer_token_env, "")
        self.crc_token = os.getenv(cfg.webhook_crc_token_env, "")
        self.api_key = os.getenv(cfg.api_key_env, "")
        self.api_secret = os.getenv(cfg.api_secret_env, "")
        self.access_token = os.getenv(cfg.access_token_env, "")
        self.access_secret = os.getenv(cfg.access_secret_env, "")
        self.http = ChannelHttpClient()

    def extract_envelope(self, payload: dict[str, Any]):
        from omnidesk_agent.channels.base import WebhookEnvelope
        events = payload.get("direct_message_events") if isinstance(payload, dict) else None
        event = events[0] if isinstance(events, list) and events and isinstance(events[0], dict) else {}
        sender = str((event.get("message_create") or {}).get("sender_id") or "unknown")
        mid = str(event.get("id") or "")
        return WebhookEnvelope(source_key=sender, sender_id=sender, message_id=mid or None, raw=payload if isinstance(payload, dict) else {})

    def crc_response(self, crc_token: str) -> dict[str, str]:
        if not self.cfg.enabled:
            raise _crc_refusal(403, "X channel is disabled")
        if not self.crc_token:
            raise _crc_refusal(503, "X webhook CRC secret is missing")
        if secret_equals(self.crc_token, os.getenv(self.cfg.webhook_secret_env, "")):
            raise _crc_refusal(503, "X webhook CRC secret must differ from the webhook signature secret")
        if not isinstance(crc_token, str) or not _X_CRC_TOKEN_RE.fullmatch(crc_token):
            raise _crc_refusal(400, "invalid crc_token")
        return {"response_token": x_signature(self.crc_token, crc_token.encode("utf-8"))}

    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload) -> None:
        from omnidesk_agent.channels.verify import env_secret, header, verify_hmac_sha256
        if not isinstance(payload, dict):
            raise PermissionError("X webhook body must be a JSON object")
        signatures = [value for value in (header(headers, "x-twitter-webhooks-signature-oauth2"), header(headers, "x-twitter-webhooks-signature")) if value]
        if signatures:
            secret = self.crc_token or env_secret(self.cfg.webhook_crc_token_env, channel=self.name)
            expected = x_signature(secret, body).encode("ascii")
            # X may send both headers, each keyed by a different secret; the one
            # keyed by the configured secret must verify.
            if not any(hmac.compare_digest(value.encode("utf-8", "surrogatepass"), expected) for value in signatures):
                raise PermissionError("invalid X webhook signature")
            return
        # Fallback for deployments with a body-signing relay in front.
        verify_hmac_sha256(body, env_secret(self.cfg.webhook_secret_env, channel=self.name), header(headers, "x-omnidesk-webhook-signature-256"), prefix="sha256=")

    def parse_webhook(self, payload: dict[str, Any]) -> list[ChannelMessage]:
        out = []
        for_user_id = str(payload.get("for_user_id") or "")
        for event in payload.get("direct_message_events", []) or []:
            if not isinstance(event, dict) or event.get("type", "message_create") != "message_create":
                continue
            sender = str(event.get("message_create", {}).get("sender_id", ""))
            if for_user_id and sender == for_user_id:
                continue  # our own outgoing DM echoed back
            if self.cfg.allowed_user_ids and sender not in self.cfg.allowed_user_ids:
                continue
            text = event.get("message_create", {}).get("message_data", {}).get("text", "")
            if text:
                out.append(ChannelMessage(channel=self.name, sender_id=sender, thread_id=f"dm:{sender}", message_id=str(event.get("id") or ""), text=text, raw=event))
        return out

    def _authorization(self, method: str, url: str) -> str:
        if self.api_key and self.api_secret and self.access_token and self.access_secret:
            return oauth1_authorization(method, url, consumer_key=self.api_key, consumer_secret=self.api_secret, token=self.access_token, token_secret=self.access_secret)
        if self.bearer_token:
            # Only an OAuth 2.0 *user-context* access token can post; an
            # app-only bearer token is refused by X. Nothing refreshes it.
            return f"Bearer {self.bearer_token}"
        raise RuntimeError("X user-context credentials are missing (X_API_KEY/X_API_SECRET/X_ACCESS_TOKEN/X_ACCESS_SECRET)")

    async def _post(self, url: str, body: dict[str, Any], idempotency_key: Any) -> Any:
        return await self.http.post(url, headers={"Authorization": self._authorization("POST", url)}, json=body, idempotency_key=idempotency_key, channel=self.name)

    async def post_text(self, text: str, **kwargs) -> dict[str, Any]:
        result = await self._post(f"{X_API_BASE}/2/tweets", {"text": text}, kwargs.get("idempotency_key"))
        data = result.data.get("data") if isinstance(result.data, dict) and isinstance(result.data.get("data"), dict) else {}
        return {"message_id": data.get("id"), "request_id": result.request_id}

    async def send_dm(self, participant_id: str, text: str, **kwargs) -> dict[str, Any]:
        if not _X_USER_ID_RE.fullmatch(participant_id):
            raise ValueError("unsupported X DM recipient: expected a numeric user id")
        result = await self._post(f"{X_API_BASE}/2/dm_conversations/with/{participant_id}/messages", {"text": text}, kwargs.get("idempotency_key"))
        data = result.data.get("data") if isinstance(result.data, dict) and isinstance(result.data.get("data"), dict) else {}
        return {"message_id": data.get("dm_event_id"), "request_id": result.request_id}

    async def send_text(self, recipient: str, text: str, **kwargs) -> dict[str, Any]:
        target = str(recipient or "")
        if target.lower() in {"public", "timeline", "tweet"}:
            return await self.post_text(text, idempotency_key=kwargs.get("idempotency_key"))
        if target.startswith("dm:"):
            return await self.send_dm(target[3:], text, idempotency_key=kwargs.get("idempotency_key"))
        raise NotImplementedError("X send_text recipient must be 'public' (post) or 'dm:<user id>' (direct message)")
