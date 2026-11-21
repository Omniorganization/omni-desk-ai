from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Mapping, cast

from fastapi import HTTPException, Request

from omnidesk_agent.config import AppConfig


class WebhookGuard:
    """Platform-aware webhook signature and replay guard.

    Public channels should use provider-native signature schemes. The generic
    OmniDesk HMAC header remains available only as a fallback for internal or
    provider-limited adapters.
    """

    def __init__(self, cfg: AppConfig, runtime):
        self.cfg = cfg
        self.runtime = runtime

    @staticmethod
    def json_body(body: bytes) -> dict:
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def channel_cfg(self, channel: str):
        return {
            "telegram": self.cfg.channels.telegram,
            "whatsapp": self.cfg.channels.whatsapp_cloud,
            "whatsapp_cloud": self.cfg.channels.whatsapp_cloud,
            "meta": self.cfg.channels.meta_graph,
            "wechat": self.cfg.channels.wechat_official,
            "wechat_official": self.cfg.channels.wechat_official,
            "dingtalk": self.cfg.channels.dingtalk,
            "lark": self.cfg.channels.lark,
            "feishu": self.cfg.channels.feishu,
            "line": self.cfg.channels.line,
            "x": self.cfg.channels.x,
            "slack": self.cfg.channels.slack,
            "discord": self.cfg.channels.discord,
            "google_chat": self.cfg.channels.google_chat,
            "signal": self.cfg.channels.signal,
            "imessage": self.cfg.channels.imessage,
            "microsoft_teams": self.cfg.channels.microsoft_teams,
            "teams": self.cfg.channels.microsoft_teams,
            "matrix": self.cfg.channels.matrix,
            "qq": self.cfg.channels.qq,
        }.get(channel)

    @staticmethod
    def env(name: str) -> str:
        return os.getenv(name, "")

    def require_secret(self, env_name: str, channel: str) -> str:
        secret = self.env(env_name)
        if not secret:
            raise PermissionError(f"{channel} webhook signature secret is not configured: {env_name}")
        return secret

    @staticmethod
    def verify_hmac_header(body: bytes, secret: str, signature: str, *, prefix: str = "sha256=") -> None:
        if not signature:
            raise PermissionError("missing webhook signature header")
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        candidate = signature[len(prefix):] if prefix and signature.startswith(prefix) else signature
        if not hmac.compare_digest(candidate, digest):
            raise PermissionError("invalid webhook signature")

    def verify_required_signature(self, channel: str, adapter, request: Request, body: bytes, payload) -> None:
        channel_cfg = self.channel_cfg(channel)
        self.require_enabled_channel(channel)
        if not getattr(self.cfg.gateway, "require_webhook_signatures", True):
            return

        adapter_verify = getattr(adapter, "verify_request", None)
        if callable(adapter_verify):
            adapter_verify(dict(request.headers), body, dict(request.query_params), payload)
            return

        headers = request.headers
        if channel == "telegram":
            secret = self.require_secret(channel_cfg.webhook_secret_env, channel)
            provided = headers.get("x-telegram-bot-api-secret-token", "")
            if not hmac.compare_digest(provided, secret):
                raise PermissionError("invalid Telegram webhook secret token")
            return

        if channel in {"whatsapp", "meta"}:
            secret = self.require_secret(channel_cfg.app_secret_env, channel)
            self.verify_hmac_header(body, secret, headers.get("x-hub-signature-256", ""), prefix="sha256=")
            return

        if channel == "line":
            signature = headers.get("x-line-signature", "")
            if not adapter.verify_signature(body, signature):
                raise PermissionError("invalid LINE webhook signature")
            return

        if channel == "wechat":
            q = request.query_params
            if not adapter.verify_signature(q.get("signature", ""), q.get("timestamp", ""), q.get("nonce", "")):
                raise PermissionError("invalid WeChat webhook signature")
            return

        if channel in {"lark", "feishu"}:
            token = getattr(adapter, "verification_token", "") or self.require_secret(channel_cfg.verification_token_env, channel)
            if not isinstance(payload, dict) or payload.get("token") != token:
                raise PermissionError(f"invalid {channel} verification token")
            secret_env = getattr(channel_cfg, "webhook_secret_env", "")
            secret = self.env(secret_env) if secret_env else ""
            signature = headers.get("x-omnidesk-webhook-signature-256", "")
            if secret or signature:
                self.verify_hmac_header(body, secret or self.require_secret(secret_env, channel), signature, prefix="sha256=")
            return

        secret_env = getattr(channel_cfg, "webhook_secret_env", "")
        secret = self.require_secret(secret_env, channel)
        self.verify_hmac_header(body, secret, headers.get("x-omnidesk-webhook-signature-256", ""), prefix="sha256=")

    def _claim_signed_nonce(self, channel: str, adapter, request: Request) -> None:
        # Call only after the signature was verified: an unverified nonce is not worth recording.
        if not hasattr(self.runtime, "webhook_security"):
            return
        extract = getattr(adapter, "signed_request_nonce", None)
        nonce = extract(dict(request.query_params)) if callable(extract) else None
        # Duck-typed (see channels.base.SignedRequestNonce) so adapters without
        # the hook, or returning None, are simply not nonce-based.
        value = getattr(nonce, "value", None)
        if isinstance(value, str) and value:
            self.runtime.webhook_security.claim_nonce(channel=channel, nonce=value, ttl_seconds=float(nonce.ttl_seconds))

    @staticmethod
    def envelope(adapter, payload):
        if hasattr(adapter, "extract_envelope"):
            return adapter.extract_envelope(payload)
        from omnidesk_agent.channels.base import WebhookEnvelope
        return WebhookEnvelope(raw=payload if isinstance(payload, dict) else {})

    def require_enabled_channel(self, channel: str) -> None:
        channel_cfg = self.channel_cfg(channel)
        if channel_cfg is None or not bool(getattr(channel_cfg, "enabled", False)):
            raise PermissionError("Webhook channel is disabled or unknown")

    async def guard(self, channel: str, adapter, request: Request, payload=None) -> tuple[bytes, object]:
        try:
            self.require_enabled_channel(channel)
            body = await request.body()
            actual_payload = payload if payload is not None else self.json_body(body)
            self.verify_required_signature(channel, adapter, request, body, actual_payload)
            envelope = self.envelope(adapter, actual_payload)
            if getattr(self.cfg.gateway, "require_webhook_signatures", True):
                # Consume the signed nonce before recording the message digest so
                # a rejected replay cannot burn the id of a later real message.
                self._claim_signed_nonce(channel, adapter, request)
            if hasattr(self.runtime, "webhook_security"):
                self.runtime.webhook_security.guard(
                    channel=channel,
                    body=body,
                    source_key=getattr(envelope, "source_key", None) or "unknown",
                    message_id=getattr(envelope, "message_id", None),
                    timestamp=getattr(envelope, "timestamp", None),
                )
            return body, envelope
        except PermissionError as exc:
            metrics = getattr(self.runtime, "metrics", None)
            inc = getattr(metrics, "inc", None)
            if callable(inc):
                inc("omnidesk_webhook_signature_failures_total", channel=channel, reason=str(exc)[:80])
            raise HTTPException(status_code=403, detail=str(exc)) from exc


    async def verify_signed_handshake(self, channel: str, adapter, request: Request) -> None:
        """Verify a signed GET handshake (e.g. WeChat echostr) and consume its nonce.

        The handshake carries the same kind of signed timestamp/nonce as a
        delivery; left unconsumed, a captured handshake query could be reused
        to sign a forged POST.
        """
        try:
            self.require_enabled_channel(channel)
            adapter.verify_request(dict(request.headers), b"", dict(request.query_params), None)
            self._claim_signed_nonce(channel, adapter, request)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc


def enqueue_webhook_message(runtime, message) -> dict:
    metrics = getattr(runtime, "metrics", None)
    inc = getattr(metrics, "inc", None)
    if message is None:
        return {"ok": True, "ignored": True}
    if callable(inc):
        inc("omnidesk_webhook_enqueue_attempts_total", channel=getattr(message, "channel", "unknown"))
    source_key = message.thread_id or message.sender_id or "unknown"
    try:
        job = runtime.job_queue.enqueue(message, source_key=source_key)
    except Exception:
        if callable(inc):
            inc("omnidesk_webhook_enqueue_failures_total", channel=getattr(message, "channel", "unknown"))
        raise
    return {"ok": True, "queued": True, "job_id": job["job_id"], "created": job["created"]}


def enqueue_webhook_messages(runtime, messages: list) -> dict:
    jobs = [enqueue_webhook_message(runtime, message) for message in messages]
    return {"ok": True, "queued": True, "count": len(messages), "jobs": jobs}


# Route channel name -> (channels.<config attribute>, runtime.adapters key).
SELF_TEST_CHANNELS: dict[str, tuple[str, str]] = {
    "telegram": ("telegram", "telegram"),
    "whatsapp": ("whatsapp_cloud", "whatsapp_cloud"),
    "meta": ("meta_graph", "meta_graph"),
    "wechat": ("wechat_official", "wechat_official"),
    "dingtalk": ("dingtalk", "dingtalk"),
    "lark": ("lark", "lark"),
    "feishu": ("feishu", "feishu"),
    "line": ("line", "line"),
    "x": ("x", "x"),
    "slack": ("slack", "slack"),
    "discord": ("discord", "discord"),
    "google_chat": ("google_chat", "google_chat"),
    "signal": ("signal", "signal"),
    "imessage": ("imessage", "imessage"),
    "microsoft_teams": ("microsoft_teams", "microsoft_teams"),
    "matrix": ("matrix", "matrix"),
    "qq": ("qq", "qq"),
}


class _SelfTestRequest:
    def __init__(self, headers: dict[str, str], query: dict[str, str]):
        from starlette.datastructures import Headers, QueryParams

        self.headers = Headers(headers)
        self.query_params = QueryParams(query)


# Headers, query parameters and body of one self-test delivery.
_Delivery = tuple[dict[str, str], dict[str, str], bytes]


def _signature_checks(
    channel: str, channel_cfg: Any, env: Mapping[str, str], production: bool, body: bytes
) -> tuple[list[tuple[str, ...]], list[_Delivery]]:
    """The secrets a channel's verify_request reads, and one wrongly signed delivery per comparison it makes.

    Each entry of the first list names the variables that can satisfy one check;
    one of them must be set. Variables used only to send, for a GET handshake, or
    by modes no adapter implements (WeChat's AES key, Lark's encrypt key) are not
    required. Each delivery is in the provider's format, every header, timestamp,
    nonce and query parameter set and fresh, and signed with a random secret or
    key, so the adapter refuses it at the signature comparison rather than for a
    missing field. The deliveries go to every mode the adapter has; a mode the
    channel has not configured must refuse its delivery too.
    """
    key = secrets.token_bytes(32)  # the wrong secret for every HMAC below
    now = str(int(time.time()))

    def bridge(data: bytes = body) -> _Delivery:  # the OmniDesk signed-bridge header
        return {"x-omnidesk-webhook-signature-256": "sha256=" + hmac.new(key, data, hashlib.sha256).hexdigest()}, {}, data

    def bearer() -> _Delivery:
        return {"authorization": "Bearer " + secrets.token_urlsafe(32)}, {}, body

    c = channel_cfg
    if channel == "telegram":
        return [(c.webhook_secret_env,)], [({"x-telegram-bot-api-secret-token": secrets.token_urlsafe(32)}, {}, body)]
    if channel in {"whatsapp", "meta"}:
        return [(c.app_secret_env,)], [({"x-hub-signature-256": "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()}, {}, body)]
    if channel == "wechat":
        from omnidesk_agent.validation.webhook_signatures import wechat_signature

        # Plaintext mode signs the sorted token, timestamp and nonce; no adapter mode reads the AES key.
        nonce = str(secrets.randbelow(10**10))
        query = {"signature": wechat_signature(os.urandom(16).hex(), now, nonce), "timestamp": now, "nonce": nonce, "openid": "o_selftest"}
        return [(c.token_env,)], [({}, query, body)]
    if channel == "line":
        signature = base64.b64encode(hmac.new(key, body, hashlib.sha256).digest()).decode("ascii")
        return [(c.channel_secret_env,)], [({"x-line-signature": signature}, {}, body)]
    if channel == "slack":
        signature = "v0=" + hmac.new(key, b"v0:" + now.encode("ascii") + b":" + body, hashlib.sha256).hexdigest()
        return [(c.signing_secret_env,)], [({"x-slack-request-timestamp": now, "x-slack-signature": signature}, {}, body)]
    if channel == "discord":
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        # Discord's own Ed25519 signature, or the bridge HMAC: either key is enough.
        signature = Ed25519PrivateKey.generate().sign(now.encode("ascii") + body).hex()
        ed25519: _Delivery = ({"x-signature-ed25519": signature, "x-signature-timestamp": now}, {}, body)
        return [(c.public_key_env, c.webhook_secret_env)], [ed25519, bridge()]
    if channel in {"lark", "feishu"}:
        # The payload's verification token is checked first, then the bridge HMAC once its
        # secret is set; that delivery carries the configured token so that it gets that far.
        def event(token: str) -> bytes:
            payload = {"uuid": secrets.token_hex(16), "token": token, "ts": now, "type": "event_callback", "event": {}}
            return json.dumps(payload).encode("utf-8")

        configured_token = str(env.get(c.verification_token_env, ""))
        return [(c.verification_token_env,)], [({}, {}, event(secrets.token_urlsafe(24))), bridge(event(configured_token))]
    if channel == "microsoft_teams":
        return [(c.inbound_bearer_token_env, c.webhook_secret_env)], [bearer(), bridge()]
    if channel == "matrix":
        return [(c.webhook_token_env, c.webhook_secret_env)], [bearer(), bridge()]
    if channel == "google_chat":
        from omnidesk_agent.channels.channel_auth import GOOGLE_CHAT_ISSUER, google_chat_requires_jwt

        # A project number turns on Google's signed bearer JWT, which needs no shared secret;
        # without one, the channel token (outside production) or the bridge HMAC.
        required: list[tuple[str, ...]] = []
        if not (c.project_number or google_chat_requires_jwt(c, production=production)):
            required = [(c.webhook_secret_env,) if production else (c.channel_token_env, c.webhook_secret_env)]
        # Google's JWT format, but no key Google publishes: refused at the key lookup,
        # since reaching the RS256 comparison would need one of Google's current key ids.
        claims = {"iss": GOOGLE_CHAT_ISSUER, "aud": str(c.project_number or ""), "iat": int(now), "exp": int(now) + 3600}
        segments = [
            json.dumps({"alg": "RS256", "kid": secrets.token_hex(20), "typ": "JWT"}).encode("utf-8"),
            json.dumps(claims).encode("utf-8"),
            os.urandom(256),
        ]
        jwt = ".".join(base64.urlsafe_b64encode(segment).rstrip(b"=").decode("ascii") for segment in segments)
        channel_token: _Delivery = ({"x-goog-channel-token": secrets.token_urlsafe(32)}, {}, body)
        return required, [({"authorization": "Bearer " + jwt}, {}, body), channel_token, bridge()]
    # dingtalk, x, signal, imessage and qq accept the bridge HMAC only.
    return [(c.webhook_secret_env,)], [bridge()]


def _delivery_outcome(guard: WebhookGuard, channel: str, adapter: Any, headers: dict[str, str], query: dict[str, str], body: bytes) -> str:
    try:
        request = cast(Request, _SelfTestRequest(headers, query))  # headers and query_params only
        guard.verify_required_signature(channel, adapter, request, body, json.loads(body))
    except Exception as exc:  # any refusal counts; the route turns it into 403 or 500
        return f"refused ({type(exc).__name__})"
    return "ACCEPTED"


def self_test_webhook_signatures(cfg: AppConfig, runtime, environ=None) -> dict:
    """Check, through the live verification path, that inbound webhooks need a valid signature.

    For every enabled inbound channel: the secrets its verification reads must be
    set (see _signature_checks), and an unsigned delivery and wrongly signed ones
    in the provider's format must all be refused by
    WebhookGuard.verify_required_signature, the code the webhook routes run.
    Accepting a correctly signed delivery needs each provider's signing key and is
    covered by the channel tests, not here.
    """
    from omnidesk_agent.validation.production import is_production_mode

    env = os.environ if environ is None else environ
    guard = WebhookGuard(cfg, runtime)
    enforced = bool(getattr(cfg.gateway, "require_webhook_signatures", True))
    production = is_production_mode(cfg, env)
    channels: dict[str, dict] = {}
    for route_channel, (config_attr, adapter_key) in SELF_TEST_CHANNELS.items():
        channel_cfg = getattr(cfg.channels, config_attr, None)
        if channel_cfg is None or not bool(getattr(channel_cfg, "enabled", False)):
            continue
        adapter = getattr(runtime, "adapters", {}).get(adapter_key)
        body = json.dumps({"omnidesk_selftest": secrets.token_hex(8)}).encode("utf-8")
        required, forged = _signature_checks(route_channel, channel_cfg, env, production, body)
        missing = [" or ".join(names) for names in required if not any(str(env.get(name, "")).strip() for name in names)]
        wrong = {_delivery_outcome(guard, route_channel, adapter, *delivery) for delivery in forged}
        results = {
            "unsigned": _delivery_outcome(guard, route_channel, adapter, {}, {}, body),
            "wrong_secret": "ACCEPTED" if "ACCEPTED" in wrong else ", ".join(sorted(wrong)),
        }
        ok = enforced and not missing and adapter is not None and all(v.startswith("refused") for v in results.values())
        channels[route_channel] = {"ok": ok, "missing_env": missing, "adapter": adapter is not None, **results}
    ok = enforced and all(item["ok"] for item in channels.values())
    return {
        "ok": ok,
        "require_webhook_signatures": enforced,
        "channels": channels,
        "note": "no inbound channel is enabled" if not channels else "unsigned and wrongly signed deliveries must be refused",
    }
