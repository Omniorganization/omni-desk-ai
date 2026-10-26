from __future__ import annotations

import hashlib
import hmac
import json
import os

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
