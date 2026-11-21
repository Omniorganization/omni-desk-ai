from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from typing import Any, Optional
from urllib.parse import urlsplit
from uuid import uuid4

from omnidesk_agent.channels.base import WebhookEnvelope
from omnidesk_agent.channels.channel_auth import CertFetcher, GoogleChatJwtVerifier, bearer_token, google_chat_requires_jwt, secret_equals
from omnidesk_agent.channels.bot_framework import BOT_FRAMEWORK_SCOPE, BotFrameworkJwtVerifier, token_endpoint as bot_framework_token_endpoint
from omnidesk_agent.channels.http_client import ChannelHttpClient, ChannelHttpError
from omnidesk_agent.channels.idempotency import provider_retry_key
from omnidesk_agent.channels.provider_errors import before_send, ensure_provider_success
from omnidesk_agent.channels.verify import env_secret, header, verify_hmac_sha256
from omnidesk_agent.config import (
    DiscordConfig,
    GoogleChatConfig,
    IMessageConfig,
    MatrixConfig,
    MicrosoftTeamsConfig,
    QQConfig,
    SignalConfig,
    SlackConfig,
)
from omnidesk_agent.core.models import ChannelMessage


_JSON_HEADERS = {"Content-Type": "application/json"}


def _text_from_payload(payload: Any, *paths: tuple[str, ...]) -> str:
    for path in paths:
        current = payload
        for part in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(part)
        if isinstance(current, str) and current.strip():
            return current.strip()
    return ""


def verify_ed25519(public_key_hex: str, message: bytes, signature_hex: str, *, channel: str) -> None:
    """Verify a hex Ed25519 signature with a hex public key (Discord, QQ)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not signature_hex:
        raise PermissionError(f"missing {channel} Ed25519 signature")
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        signature = bytes.fromhex(signature_hex)
    except ValueError as exc:
        raise PermissionError(f"malformed {channel} Ed25519 signature or public key") from exc
    try:
        key.verify(signature, message)
    except InvalidSignature as exc:
        raise PermissionError(f"invalid {channel} Ed25519 signature") from exc


def _require_env(env_name: str, *, channel: str) -> str:
    value = os.getenv(env_name, "")
    if not value:
        raise RuntimeError(f"{channel} required environment variable is missing: {env_name}")
    return value


class SlackChannel:
    """Slack Events API + chat.postMessage adapter.

    Incoming requests use Slack's signing-secret scheme when enabled. The adapter
    accepts URL verification challenges, message events, app mentions, and slash
    command style text payloads. Outbound sends use chat.postMessage.
    """

    name = "slack"

    def __init__(self, cfg: SlackConfig):
        self.cfg = cfg
        self.bot_token = os.getenv(cfg.bot_token_env, "")
        self.signing_secret = os.getenv(cfg.signing_secret_env, "")
        self.http = ChannelHttpClient()

    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload: Any) -> None:
        secret = self.signing_secret or env_secret(self.cfg.signing_secret_env, channel=self.name)
        ts = header(headers, "x-slack-request-timestamp")
        sig = header(headers, "x-slack-signature")
        if not ts or not sig:
            raise PermissionError("missing Slack signature headers")
        try:
            timestamp = int(ts)
        except ValueError as exc:
            raise PermissionError("invalid Slack timestamp") from exc
        if abs(int(time.time()) - timestamp) > self.cfg.max_timestamp_skew_seconds:
            raise PermissionError("stale Slack webhook timestamp")
        base = b"v0:" + ts.encode("utf-8") + b":" + body
        expected = "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise PermissionError("invalid Slack signature")

    def extract_envelope(self, payload: dict[str, Any]) -> WebhookEnvelope:
        event = payload.get("event") or payload
        sender = str(event.get("user") or payload.get("user_id") or "unknown")
        source = str(event.get("channel") or payload.get("channel_id") or sender)
        mid = str(event.get("client_msg_id") or event.get("event_ts") or payload.get("event_id") or "")
        ts_value = event.get("ts") or event.get("event_ts")
        try:
            ts = float(ts_value) if ts_value else None
        except (TypeError, ValueError):
            ts = None
        return WebhookEnvelope(source_key=source, sender_id=sender, message_id=mid or None, timestamp=ts, raw=payload)

    def parse_webhook(self, payload: dict[str, Any]) -> Optional[ChannelMessage | dict[str, str]]:
        if payload.get("type") == "url_verification":
            return {"challenge": str(payload.get("challenge", ""))}
        event = payload.get("event") or payload
        if event.get("bot_id") and not self.cfg.allow_bot_messages:
            return None
        event_type = str(event.get("type") or payload.get("command") or "")
        if event_type and event_type not in {"message", "app_mention", "/omnidesk", "slash_command"}:
            return None
        sender = str(event.get("user") or payload.get("user_id") or "")
        channel_id = str(event.get("channel") or payload.get("channel_id") or "")
        if self.cfg.allowed_user_ids and sender not in self.cfg.allowed_user_ids:
            return None
        if self.cfg.allowed_channel_ids and channel_id not in self.cfg.allowed_channel_ids:
            return None
        text = str(event.get("text") or payload.get("text") or "").strip()
        if not text:
            return None
        return ChannelMessage(channel=self.name, sender_id=sender or "unknown", thread_id=channel_id or sender, message_id=str(event.get("client_msg_id") or event.get("event_ts") or payload.get("event_id") or ""), text=text, raw=payload)

    async def send_text(self, recipient: str, text: str, **kwargs) -> dict[str, Any] | None:
        token = self.bot_token or _require_env(self.cfg.bot_token_env, channel=self.name)
        body = {"channel": recipient, "text": text}
        if kwargs.get("thread_ts"):
            body["thread_ts"] = kwargs["thread_ts"]
        result = await self.http.post("https://slack.com/api/chat.postMessage", headers={"Authorization": f"Bearer {token}", **_JSON_HEADERS}, json=body, idempotency_key=kwargs.get("idempotency_key"), channel=self.name)
        ensure_provider_success(self.name, result)
        data = result.data if isinstance(result.data, dict) else {}
        return {"message_id": data.get("ts"), "request_id": result.request_id}


class DiscordChannel:
    """Discord interactions-endpoint adapter.

    Discord signs every interaction with Ed25519 (``X-Signature-Ed25519`` over
    ``X-Signature-Timestamp`` + raw body); with ``DISCORD_PUBLIC_KEY`` set that
    signature is required and verified with ``cryptography``. Gateway (websocket)
    message events are not received; without a public key only a signed edge
    bridge (OmniDesk HMAC) is accepted.
    """

    name = "discord"

    def __init__(self, cfg: DiscordConfig):
        self.cfg = cfg
        self.bot_token = os.getenv(cfg.bot_token_env, "")
        self.webhook_secret = os.getenv(cfg.webhook_secret_env, "")
        self.public_key = os.getenv(cfg.public_key_env, "")
        self.http = ChannelHttpClient()

    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload: Any) -> None:
        if self.public_key:
            # Discord's own signature is mandatory once the public key is set;
            # Discord also probes the endpoint with bad signatures and drops it
            # unless those are refused.
            verify_ed25519(self.public_key, header(headers, "x-signature-timestamp").encode("utf-8") + body, header(headers, "x-signature-ed25519"), channel="Discord")
            return
        verify_hmac_sha256(body, self.webhook_secret or env_secret(self.cfg.webhook_secret_env, channel=self.name), header(headers, "x-omnidesk-webhook-signature-256"), prefix="sha256=")

    def extract_envelope(self, payload: dict[str, Any]) -> WebhookEnvelope:
        message = payload.get("message") or payload
        author = message.get("author") or payload.get("member", {}).get("user") or payload.get("user") or {}
        sender = str(author.get("id") or "unknown")
        source = str(message.get("channel_id") or payload.get("channel_id") or payload.get("guild_id") or sender)
        mid = str(message.get("id") or payload.get("id") or "")
        return WebhookEnvelope(source_key=source, sender_id=sender, message_id=mid or None, raw=payload)

    def parse_webhook(self, payload: dict[str, Any]) -> Optional[ChannelMessage | dict[str, Any]]:
        if payload.get("type") == 1:
            return {"type": 1}
        message = payload.get("message") or payload
        data = payload.get("data") or {}
        author = message.get("author") or payload.get("member", {}).get("user") or payload.get("user") or {}
        sender = str(author.get("id") or "")
        channel_id = str(message.get("channel_id") or payload.get("channel_id") or "")
        if self.cfg.allowed_user_ids and sender not in self.cfg.allowed_user_ids:
            return None
        if self.cfg.allowed_channel_ids and channel_id not in self.cfg.allowed_channel_ids:
            return None
        text = str(message.get("content") or data.get("name") or "").strip()
        if not text and isinstance(data.get("options"), list):
            text = " ".join(str(item.get("value", "")) for item in data["options"] if isinstance(item, dict)).strip()
        if not text:
            return None
        return ChannelMessage(channel=self.name, sender_id=sender or "unknown", thread_id=channel_id or sender, message_id=str(message.get("id") or payload.get("id") or ""), text=text, raw=payload)

    def webhook_response(self, parsed: Any, payload: Any) -> Optional[dict[str, Any]]:
        """Interaction response Discord needs within 3 seconds.

        The reply itself goes out later through the approval-gated send, so a
        command is acknowledged with an ephemeral notice and a component with
        a deferred update.
        """
        interaction_type = payload.get("type") if isinstance(payload, dict) else None
        if interaction_type in (2, 5):  # APPLICATION_COMMAND, MODAL_SUBMIT
            return {"type": 4, "data": {"content": "Received. The reply follows once it is approved.", "flags": 64}}
        if interaction_type == 3:  # MESSAGE_COMPONENT
            return {"type": 6}
        return None

    async def send_text(self, recipient: str, text: str, **kwargs) -> dict[str, Any] | None:
        token = self.bot_token or _require_env(self.cfg.bot_token_env, channel=self.name)
        url = f"https://discord.com/api/v10/channels/{recipient}/messages"
        body: dict[str, Any] = {"content": text}
        idempotency_key = kwargs.get("idempotency_key")
        if idempotency_key:
            # With enforce_nonce Discord returns the message already created
            # with this nonce (for a few minutes) instead of posting again, so
            # the transport may repeat this request; the outbox may not.
            body["nonce"] = provider_retry_key(str(idempotency_key)).replace("-", "")[:25]
            body["enforce_nonce"] = True
        result = await self.http.post(url, headers={"Authorization": f"Bot {token}", **_JSON_HEADERS}, json=body, idempotency_key=idempotency_key, channel=self.name, idempotent=bool(idempotency_key))
        data = result.data if isinstance(result.data, dict) else {}
        return {"message_id": data.get("id"), "request_id": result.request_id}


GOOGLE_CHAT_WEBHOOK_PREFIX = "https://chat.googleapis.com/v1/spaces/"
_GOOGLE_CHAT_WEBHOOK_PATH_RE = re.compile(r"/v1/spaces/([A-Za-z0-9_-]{1,128})/messages")


def google_chat_webhook_space(url: str) -> str:
    """Return "spaces/<id>" for a Google Chat incoming-webhook URL, else refuse."""
    parts = urlsplit(url)
    match = _GOOGLE_CHAT_WEBHOOK_PATH_RE.fullmatch(parts.path)
    if parts.scheme != "https" or parts.netloc != "chat.googleapis.com" or parts.fragment or not match:
        raise PermissionError("Google Chat recipient must be a https://chat.googleapis.com/ incoming webhook URL")
    return f"spaces/{match.group(1)}"


class GoogleChatChannel:
    """Google Chat HTTP endpoint + incoming-webhook adapter.

    Inbound requests carry a bearer JWT signed by chat@system.gserviceaccount.com
    for this project number; it is required in production unless
    ``require_google_jwt`` is explicitly false, in which case only the signed
    edge-bridge HMAC is accepted. Outside production the channel token header
    also works, and ``?token=`` only with ``allow_query_token``.
    """

    name = "google_chat"

    def __init__(self, cfg: GoogleChatConfig, *, production: bool = False, cert_fetcher: Optional[CertFetcher] = None):
        self.cfg = cfg
        self.production = production
        self.require_jwt = google_chat_requires_jwt(cfg, production=production)
        self.jwt_verifier = GoogleChatJwtVerifier(cfg.project_number, cert_fetcher=cert_fetcher) if cfg.project_number else None
        if self.jwt_verifier is not None and cfg.enabled:
            self.jwt_verifier.prefetch()
        self.webhook_secret = os.getenv(cfg.webhook_secret_env, "")
        self.incoming_webhook_url = os.getenv(cfg.incoming_webhook_url_env, "")
        self.allowed_outbound_spaces = {
            space if space.startswith("spaces/") else f"spaces/{space}" for space in (str(item).strip() for item in cfg.allowed_outbound_spaces) if space
        }
        self.http = ChannelHttpClient()

    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload: Any) -> None:
        query_token = query_params.get("token", "")
        if query_token and (self.production or not self.cfg.allow_query_token):
            raise PermissionError("Google Chat query-string token authentication is disabled; use the Google-signed bearer token")
        bearer = bearer_token(headers)
        if bearer and self.jwt_verifier is not None:
            self.jwt_verifier.verify(bearer)
            return
        if self.require_jwt:
            raise PermissionError("Google Chat request has no verifiable Google-signed bearer token")
        # The static channel token does not cover the body, so production
        # without the JWT accepts only the signed-bridge HMAC.
        if not self.production:
            token = header(headers, "x-goog-channel-token") or query_token
            expected = os.getenv(self.cfg.channel_token_env, "")
            if expected and token:
                if not secret_equals(token, expected):
                    raise PermissionError("invalid Google Chat channel token")
                return
        verify_hmac_sha256(body, self.webhook_secret or env_secret(self.cfg.webhook_secret_env, channel=self.name), header(headers, "x-omnidesk-webhook-signature-256"), prefix="sha256=")

    def extract_envelope(self, payload: dict[str, Any]) -> WebhookEnvelope:
        msg = payload.get("message") or payload
        user = payload.get("user") or msg.get("sender") or {}
        sender = str(user.get("name") or user.get("displayName") or "unknown")
        source = str((payload.get("space") or msg.get("space") or {}).get("name") or sender)
        mid = str(msg.get("name") or payload.get("eventTime") or "")
        return WebhookEnvelope(source_key=source, sender_id=sender, message_id=mid or None, raw=payload)

    def parse_webhook(self, payload: dict[str, Any]) -> Optional[ChannelMessage]:
        msg = payload.get("message") or payload
        user = payload.get("user") or msg.get("sender") or {}
        sender = str(user.get("name") or user.get("displayName") or "")
        space = str((payload.get("space") or msg.get("space") or {}).get("name") or "")
        if self.cfg.allowed_user_names and sender not in self.cfg.allowed_user_names:
            return None
        if self.cfg.allowed_space_names and space not in self.cfg.allowed_space_names:
            return None
        text = str(msg.get("argumentText") or msg.get("text") or "").strip()
        if not text:
            return None
        return ChannelMessage(channel=self.name, sender_id=sender or "unknown", thread_id=space or sender, message_id=str(msg.get("name") or ""), text=text, raw=payload)

    def webhook_response(self, parsed: Any, payload: Any) -> dict[str, Any]:
        """Google Chat posts a synchronous response body as a reply message; ``{}`` means no reply.

        The message is queued and any reply goes out later through the
        approval-gated send, so the HTTP response must not be our job JSON.
        """
        return {}

    async def send_text(self, recipient: str, text: str, **kwargs) -> dict[str, Any] | None:
        # Never post to a caller-chosen URL (kwargs or an LLM-chosen recipient):
        # use the operator-configured webhook, or a recipient that is itself a
        # Google Chat incoming-webhook URL for an allowlisted space.
        recipient = str(recipient or "")
        if self.incoming_webhook_url:
            # The approver saw ``recipient``; with a configured webhook the
            # message lands in that webhook's space, so the recipient has to
            # name that space (or be the webhook URL itself).
            configured_space = google_chat_webhook_space(self.incoming_webhook_url)
            named = recipient if recipient.startswith("spaces/") else (google_chat_webhook_space(recipient) if recipient.startswith("https://") else "")
            if named != configured_space:
                raise before_send(PermissionError(f"Google Chat recipient must be {configured_space}, the space of GOOGLE_CHAT_WEBHOOK_URL"), "bad_request")
            url = self.incoming_webhook_url
        else:
            url = recipient
            if not url.startswith("https://"):
                raise before_send(RuntimeError("Google Chat incoming webhook URL is missing"), "config_error")
            if google_chat_webhook_space(url) not in self.allowed_outbound_spaces:
                raise before_send(PermissionError("Google Chat space is not in channels.google_chat.allowed_outbound_spaces"), "bad_request")
        result = await self.http.post(str(url), json={"text": text}, idempotency_key=kwargs.get("idempotency_key"), channel=self.name)
        data = result.data if isinstance(result.data, dict) else {}
        return {"message_id": data.get("name"), "request_id": result.request_id}


class SignalChannel:
    """Signal adapter for a self-hosted signal-cli REST bridge."""

    name = "signal"

    def __init__(self, cfg: SignalConfig):
        self.cfg = cfg
        self.rest_url = cfg.rest_url.rstrip("/") if cfg.rest_url else ""
        self.account_number = cfg.account_number or os.getenv(cfg.account_number_env, "")
        self.token = os.getenv(cfg.rest_token_env, "")
        self.webhook_secret = os.getenv(cfg.webhook_secret_env, "")
        self.http = ChannelHttpClient()

    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload: Any) -> None:
        verify_hmac_sha256(body, self.webhook_secret or env_secret(self.cfg.webhook_secret_env, channel=self.name), header(headers, "x-omnidesk-webhook-signature-256"), prefix="sha256=")

    def extract_envelope(self, payload: dict[str, Any]) -> WebhookEnvelope:
        envelope = payload.get("envelope") or payload
        data = envelope.get("dataMessage") or envelope.get("syncMessage", {}).get("sentMessage") or {}
        sender = str(envelope.get("sourceNumber") or envelope.get("source") or data.get("destination") or "unknown")
        mid = str(envelope.get("timestamp") or data.get("timestamp") or "")
        ts = float(mid) / 1000.0 if mid.isdigit() else None
        return WebhookEnvelope(source_key=sender, sender_id=sender, message_id=mid or None, timestamp=ts, raw=payload)

    def parse_webhook(self, payload: dict[str, Any]) -> Optional[ChannelMessage]:
        envelope = payload.get("envelope") or payload
        data = envelope.get("dataMessage") or envelope.get("syncMessage", {}).get("sentMessage") or {}
        sender = str(envelope.get("sourceNumber") or envelope.get("source") or "")
        if self.cfg.allowed_senders and sender not in self.cfg.allowed_senders:
            return None
        text = str(data.get("message") or payload.get("message") or "").strip()
        if not text:
            return None
        mid = str(envelope.get("timestamp") or data.get("timestamp") or "")
        return ChannelMessage(channel=self.name, sender_id=sender or "unknown", thread_id=sender, message_id=mid, text=text, raw=payload)

    async def send_text(self, recipient: str, text: str, **kwargs) -> dict[str, Any] | None:
        if not self.rest_url:
            raise RuntimeError("Signal REST bridge URL is missing")
        account = self.account_number or _require_env(self.cfg.account_number_env, channel=self.name)
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        body = {"message": text, "number": account, "recipients": [recipient]}
        result = await self.http.post(f"{self.rest_url}/v2/send", headers=headers, json=body, idempotency_key=kwargs.get("idempotency_key"), channel=self.name)
        data = result.data or {}
        return {"message_id": data.get("timestamp"), "request_id": result.request_id}


class IMessageChannel:
    """iMessage adapter for a local macOS Messages relay.

    Apple does not provide a server-side iMessage bot API. This adapter expects a
    trusted, local relay that performs foreground/user-approved Messages.app
    actions and signs inbound webhooks back to OmniDesk.
    """

    name = "imessage"

    def __init__(self, cfg: IMessageConfig):
        self.cfg = cfg
        self.relay_url = cfg.relay_url.rstrip("/") if cfg.relay_url else ""
        self.relay_token = os.getenv(cfg.relay_token_env, "")
        self.webhook_secret = os.getenv(cfg.webhook_secret_env, "")
        self.http = ChannelHttpClient()

    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload: Any) -> None:
        verify_hmac_sha256(body, self.webhook_secret or env_secret(self.cfg.webhook_secret_env, channel=self.name), header(headers, "x-omnidesk-webhook-signature-256"), prefix="sha256=")

    def extract_envelope(self, payload: dict[str, Any]) -> WebhookEnvelope:
        sender = str(payload.get("handle") or payload.get("sender") or "unknown")
        source = str(payload.get("chat_id") or sender)
        mid = str(payload.get("guid") or payload.get("message_id") or "")
        ts_value = payload.get("timestamp")
        try:
            ts = float(ts_value) if ts_value else None
        except (TypeError, ValueError):
            ts = None
        return WebhookEnvelope(source_key=source, sender_id=sender, message_id=mid or None, timestamp=ts, raw=payload)

    def parse_webhook(self, payload: dict[str, Any]) -> Optional[ChannelMessage]:
        sender = str(payload.get("handle") or payload.get("sender") or "")
        if self.cfg.allowed_handles and sender not in self.cfg.allowed_handles:
            return None
        text = str(payload.get("text") or payload.get("body") or "").strip()
        if not text:
            return None
        return ChannelMessage(channel=self.name, sender_id=sender or "unknown", thread_id=str(payload.get("chat_id") or sender), message_id=str(payload.get("guid") or payload.get("message_id") or ""), text=text, raw=payload)

    async def send_text(self, recipient: str, text: str, **kwargs) -> dict[str, Any] | None:
        if not self.relay_url:
            raise RuntimeError("iMessage relay URL is missing")
        headers = {"Authorization": f"Bearer {self.relay_token}"} if self.relay_token else {}
        body = {"recipient": recipient, "text": text, "require_foreground_confirmation": self.cfg.require_foreground_confirmation}
        result = await self.http.post(f"{self.relay_url}/messages", headers=headers, json=body, idempotency_key=kwargs.get("idempotency_key"), channel=self.name)
        data = result.data or {}
        return {"message_id": data.get("message_id") or data.get("guid"), "request_id": result.request_id}


class MicrosoftTeamsChannel:
    """Microsoft Teams through the Bot Framework.

    With ``MICROSOFT_TEAMS_APP_ID`` set, every inbound activity must carry a
    Bot Connector JWT (``channels.bot_framework``) and sends use an OAuth
    client-credentials token for ``MICROSOFT_TEAMS_APP_PASSWORD``. Without it
    the legacy static inbound bearer or bridge HMAC and a static bot token are
    used, which the real Bot Connector cannot work with.
    """

    name = "microsoft_teams"

    def __init__(self, cfg: MicrosoftTeamsConfig, *, key_fetcher: Optional[CertFetcher] = None):
        self.cfg = cfg
        self.bot_token = os.getenv(cfg.bot_token_env, "")
        self.webhook_secret = os.getenv(cfg.webhook_secret_env, "")
        self.app_id = os.getenv(cfg.app_id_env, "")
        self.app_password = os.getenv(cfg.app_password_env, "")
        self.tenant_id = os.getenv(cfg.tenant_id_env, "")
        self.jwt_verifier = BotFrameworkJwtVerifier(self.app_id, key_fetcher=key_fetcher) if self.app_id else None
        if self.jwt_verifier is not None and cfg.enabled:
            self.jwt_verifier.prefetch()
        self._access_token: Optional[tuple[str, float]] = None
        self.http = ChannelHttpClient()

    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload: Any) -> None:
        if self.jwt_verifier is not None:
            if not isinstance(payload, dict):
                raise PermissionError("Teams activity must be a JSON object")
            self.jwt_verifier.verify(bearer_token(headers), service_url=str(payload.get("serviceUrl") or ""), channel_id=str(payload.get("channelId") or ""))
            return
        bearer = header(headers, "authorization")
        expected = os.getenv(self.cfg.inbound_bearer_token_env, "")
        if expected and bearer:
            if not hmac.compare_digest(bearer.removeprefix("Bearer "), expected):
                raise PermissionError("invalid Teams inbound bearer token")
            return
        verify_hmac_sha256(body, self.webhook_secret or env_secret(self.cfg.webhook_secret_env, channel=self.name), header(headers, "x-omnidesk-webhook-signature-256"), prefix="sha256=")

    def extract_envelope(self, payload: dict[str, Any]) -> WebhookEnvelope:
        sender = str((payload.get("from") or {}).get("id") or "unknown")
        conv = str((payload.get("conversation") or {}).get("id") or sender)
        mid = str(payload.get("id") or "")
        return WebhookEnvelope(source_key=conv, sender_id=sender, message_id=mid or None, raw=payload)

    def parse_webhook(self, payload: dict[str, Any]) -> Optional[ChannelMessage]:
        if payload.get("type") != "message":
            return None
        sender = str((payload.get("from") or {}).get("id") or "")
        conv = str((payload.get("conversation") or {}).get("id") or "")
        if self.cfg.allowed_user_ids and sender not in self.cfg.allowed_user_ids:
            return None
        if self.cfg.allowed_conversation_ids and conv not in self.cfg.allowed_conversation_ids:
            return None
        text = str(payload.get("text") or "").strip()
        if not text:
            return None
        return ChannelMessage(channel=self.name, sender_id=sender or "unknown", thread_id=conv or sender, message_id=str(payload.get("id") or ""), text=text, raw=payload)

    async def _bot_token(self, *, refresh: bool = False) -> str:
        if not (self.app_id and self.app_password):
            return self.bot_token or _require_env(self.cfg.bot_token_env, channel=self.name)
        if not refresh and self._access_token and self._access_token[1] > time.time() + 60:
            return self._access_token[0]
        result = await self.http.post(
            bot_framework_token_endpoint(self.tenant_id),
            data={"grant_type": "client_credentials", "client_id": self.app_id, "client_secret": self.app_password, "scope": BOT_FRAMEWORK_SCOPE},
            channel=self.name,
            idempotent=True,
        )
        data = result.data if isinstance(result.data, dict) else {}
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise ChannelHttpError("Teams token response carried no access_token", channel=self.name, request_sent=True, category="config_error")
        try:
            expires_in = int(data.get("expires_in", 3599))
        except (TypeError, ValueError):
            expires_in = 3599
        self._access_token = (token, time.time() + expires_in)
        return token

    async def send_text(self, recipient: str, text: str, **kwargs) -> dict[str, Any] | None:
        # The bot bearer token is sent to service_url, so it is pinned to the
        # configured value; a caller-supplied service_url kwarg is ignored.
        service_url = self.cfg.service_url
        if not service_url:
            raise RuntimeError("Teams service_url is missing")
        recipient = str(recipient or "")
        if not recipient or any(ch in recipient for ch in "/?#") or any(ch.isspace() for ch in recipient):
            raise PermissionError("Teams recipient must be a Bot Framework conversation id")
        url = f"{str(service_url).rstrip('/')}/v3/conversations/{recipient}/activities"
        body = {"type": "message", "text": text}
        for attempt in range(2):
            token = await self._bot_token(refresh=attempt > 0)
            try:
                result = await self.http.post(url, headers={"Authorization": f"Bearer {token}", **_JSON_HEADERS}, json=body, idempotency_key=kwargs.get("idempotency_key"), channel=self.name)
            except ChannelHttpError as exc:
                if attempt == 0 and exc.status_code == 401 and self.app_password:
                    # The connector refused the token, so nothing was posted.
                    self._access_token = None
                    continue
                raise
            data = result.data if isinstance(result.data, dict) else {}
            return {"message_id": data.get("id"), "request_id": result.request_id}
        raise AssertionError("unreachable")  # pragma: no cover


class MatrixChannel:
    """Matrix hook adapter.

    The hook token is read from ``Authorization: Bearer`` or
    ``x-matrix-hook-token``; ``?token=`` / ``?access_token=`` is only honoured
    outside production with ``allow_query_token``.
    """

    name = "matrix"

    def __init__(self, cfg: MatrixConfig, *, production: bool = False):
        self.cfg = cfg
        self.production = production
        self.homeserver_url = cfg.homeserver_url.rstrip("/") if cfg.homeserver_url else ""
        self.access_token = os.getenv(cfg.access_token_env, "")
        self.webhook_secret = os.getenv(cfg.webhook_secret_env, "")
        self.http = ChannelHttpClient()

    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload: Any) -> None:
        query_token = query_params.get("token", "") or query_params.get("access_token", "")
        if query_token and (self.production or not self.cfg.allow_query_token):
            raise PermissionError("Matrix query-string token authentication is disabled; send Authorization: Bearer")
        token = bearer_token(headers) or header(headers, "x-matrix-hook-token") or query_token
        expected = os.getenv(self.cfg.webhook_token_env, "")
        if expected and token:
            if not secret_equals(token, expected):
                raise PermissionError("invalid Matrix hook token")
            return
        verify_hmac_sha256(body, self.webhook_secret or env_secret(self.cfg.webhook_secret_env, channel=self.name), header(headers, "x-omnidesk-webhook-signature-256"), prefix="sha256=")

    def extract_envelope(self, payload: dict[str, Any]) -> WebhookEnvelope:
        event = payload.get("event") or payload
        sender = str(event.get("sender") or "unknown")
        source = str(event.get("room_id") or payload.get("room_id") or sender)
        mid = str(event.get("event_id") or payload.get("event_id") or "")
        ts_value = event.get("origin_server_ts")
        ts = float(ts_value) / 1000.0 if isinstance(ts_value, (int, float)) else None
        return WebhookEnvelope(source_key=source, sender_id=sender, message_id=mid or None, timestamp=ts, raw=payload)

    def parse_webhook(self, payload: dict[str, Any]) -> Optional[ChannelMessage]:
        event = payload.get("event") or payload
        sender = str(event.get("sender") or "")
        room_id = str(event.get("room_id") or payload.get("room_id") or "")
        if self.cfg.allowed_user_ids and sender not in self.cfg.allowed_user_ids:
            return None
        if self.cfg.allowed_room_ids and room_id not in self.cfg.allowed_room_ids:
            return None
        content = event.get("content") or {}
        if content.get("msgtype") not in {None, "m.text", "m.notice"}:
            return None
        text = str(content.get("body") or event.get("body") or "").strip()
        if not text:
            return None
        return ChannelMessage(channel=self.name, sender_id=sender or "unknown", thread_id=room_id or sender, message_id=str(event.get("event_id") or ""), text=text, raw=payload)

    async def send_text(self, recipient: str, text: str, **kwargs) -> dict[str, Any] | None:
        if not self.homeserver_url:
            raise RuntimeError("Matrix homeserver_url is missing")
        token = self.access_token or _require_env(self.cfg.access_token_env, channel=self.name)
        txn_id = kwargs.get("idempotency_key") or str(uuid4())
        room_id = recipient
        url = f"{self.homeserver_url}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}"
        body = {"msgtype": "m.text", "body": text}
        result = await self.http.request("PUT", url, headers={"Authorization": f"Bearer {token}"}, json=body, channel=self.name)
        data = result.data or {}
        return {"message_id": data.get("event_id"), "request_id": result.request_id}


QQ_ACCESS_TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
# op 13 values the callback validation will sign. The handshake is itself
# signature-checked; restricting what we sign also guarantees that a signed
# "event_ts + plain_token" can never double as an event signature (those
# cover timestamp + a JSON object body).
_QQ_PLAIN_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_QQ_EVENT_TS_RE = re.compile(r"[0-9]{1,20}")
_QQ_MESSAGE_EVENTS = {"GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE", "AT_MESSAGE_CREATE"}


def qq_signing_key(secret: str):
    """Ed25519 key from the bot secret: the secret repeated to 32 bytes is the seed."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if not secret:
        raise PermissionError("QQ bot secret is not configured")
    seed = secret.encode("utf-8")
    while len(seed) < 32:
        seed += seed
    return Ed25519PrivateKey.from_private_bytes(seed[:32])


class QQChannel:
    """QQ Bot OpenAPI adapter (webhook / HTTP callback mode).

    Inbound pushes are verified with QQ's Ed25519 signature
    (``X-Signature-Ed25519`` over ``X-Signature-Timestamp`` + body, key derived
    from the bot secret) and op 13 callback validation is answered; without a
    bot secret only the OmniDesk bridge HMAC is accepted. Outbound uses an
    app access token. Recipient prefixes select the surface:
    - group:<group_openid> sends to QQ group OpenAPI
    - channel:<channel_id> sends to guild/channel messages
    - c2c:<openid> sends to C2C OpenAPI
    """

    name = "qq"

    def __init__(self, cfg: QQConfig):
        self.cfg = cfg
        self.bot_app_id = os.getenv(cfg.bot_app_id_env, "")
        self.bot_secret = os.getenv(cfg.bot_secret_env, "")
        self.bot_token = os.getenv(cfg.bot_token_env, "")
        self.webhook_secret = os.getenv(cfg.webhook_secret_env, "")
        self.api_base = cfg.api_base.rstrip("/")
        self._access_token: Optional[tuple[str, float]] = None
        self.http = ChannelHttpClient()

    def _public_key_hex(self) -> str:
        from cryptography.hazmat.primitives import serialization

        public = qq_signing_key(self.bot_secret).public_key()
        return public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()

    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload: Any) -> None:
        if self.bot_secret:
            if not isinstance(payload, dict):
                raise PermissionError("QQ callback body must be a JSON object")
            # Every callback, the op 13 URL validation included, carries QQ's
            # signature: Tencent's own SDK (botgo interaction/webhook) verifies
            # it before answering op 13, so an unsigned handshake gets no echo.
            timestamp = header(headers, "x-signature-timestamp")
            if not (timestamp.isascii() and timestamp.isdigit()):
                raise PermissionError("missing or invalid QQ X-Signature-Timestamp")
            verify_ed25519(self._public_key_hex(), timestamp.encode("utf-8") + body, header(headers, "x-signature-ed25519"), channel="QQ")
            return
        verify_hmac_sha256(body, self.webhook_secret or env_secret(self.cfg.webhook_secret_env, channel=self.name), header(headers, "x-omnidesk-webhook-signature-256"), prefix="sha256=")

    def callback_validation(self, payload: dict[str, Any]) -> dict[str, str]:
        """Answer op 13: sign event_ts + plain_token with the bot's Ed25519 key."""
        data = payload.get("d") if isinstance(payload.get("d"), dict) else {}
        plain_token = data.get("plain_token")
        event_ts = data.get("event_ts")
        if not isinstance(plain_token, str) or not _QQ_PLAIN_TOKEN_RE.fullmatch(plain_token):
            raise PermissionError("invalid QQ plain_token")
        if not isinstance(event_ts, str) or not _QQ_EVENT_TS_RE.fullmatch(event_ts):
            raise PermissionError("invalid QQ event_ts")
        signature = qq_signing_key(self.bot_secret).sign((event_ts + plain_token).encode("utf-8")).hex()
        return {"plain_token": plain_token, "signature": signature}

    @staticmethod
    def _message_fields(payload: dict[str, Any]) -> tuple[str, str, str, str, dict[str, Any]]:
        """(sender, source id, thread id, message id, data) of a message push."""
        data = payload.get("d") or payload.get("data") or payload
        data = data if isinstance(data, dict) else {}
        author = data.get("author") or data.get("member") or {}
        author = author if isinstance(author, dict) else {}
        sender = str(author.get("member_openid") or author.get("user_openid") or author.get("id") or author.get("user_id") or data.get("openid") or "")
        if data.get("group_openid"):
            source, thread = str(data["group_openid"]), f"group:{data['group_openid']}"
        elif data.get("channel_id"):
            source, thread = str(data["channel_id"]), f"channel:{data['channel_id']}"
        elif data.get("guild_id"):
            source, thread = str(data["guild_id"]), str(data["guild_id"])
        else:
            source, thread = sender, (f"c2c:{sender}" if sender else "")
        mid = str(data.get("id") or data.get("msg_id") or payload.get("id") or "")
        return sender, source, thread, mid, data

    def extract_envelope(self, payload: dict[str, Any]) -> WebhookEnvelope:
        sender, source, _thread, mid, _data = self._message_fields(payload)
        return WebhookEnvelope(source_key=source or sender or "unknown", sender_id=sender or "unknown", message_id=mid or None, raw=payload)

    def parse_webhook(self, payload: dict[str, Any]) -> Optional[ChannelMessage | dict[str, Any]]:
        if payload.get("op") == 13:
            if not self.bot_secret:
                return {"ok": True, "ignored": True, "event": "qq_callback_ack"}
            return self.callback_validation(payload)
        event_type = payload.get("t")
        if event_type is not None and event_type not in _QQ_MESSAGE_EVENTS:
            return None
        sender, source, thread, mid, data = self._message_fields(payload)
        if self.cfg.allowed_user_ids and sender not in self.cfg.allowed_user_ids:
            return None
        if self.cfg.allowed_source_ids and source not in self.cfg.allowed_source_ids:
            return None
        text = str(data.get("content") or data.get("text") or "").strip()
        if not text:
            return None
        return ChannelMessage(channel=self.name, sender_id=sender or "unknown", thread_id=thread or source or sender, message_id=mid, text=text, raw=payload)

    def webhook_response(self, parsed: Any, payload: Any) -> Optional[dict[str, Any]]:
        """Body QQ expects back: the op 13 answer, otherwise the op 12 HTTP callback ACK.

        Only called for a verified request. The op 13 answer is a pure function
        of that signed request, so it is rebuilt from ``payload`` when no parse
        result is passed (for example for a verified duplicate delivery).
        """
        if isinstance(parsed, dict) and "plain_token" in parsed:
            return parsed
        if not self.bot_secret or not isinstance(payload, dict):
            return None
        if payload.get("op") == 13:
            return self.callback_validation(payload)
        if payload.get("op") == 0:
            return {"op": 12, "d": 0}
        return None

    async def _auth_header(self, *, refresh: bool = False) -> str:
        app_id = self.bot_app_id or os.getenv(self.cfg.bot_app_id_env, "")
        if self.bot_secret:
            if refresh or not self._access_token or self._access_token[1] <= time.time() + 60:
                if not app_id:
                    raise RuntimeError(f"qq required environment variable is missing: {self.cfg.bot_app_id_env}")
                result = await self.http.post(QQ_ACCESS_TOKEN_URL, json={"appId": app_id, "clientSecret": self.bot_secret}, channel=self.name, idempotent=True)
                data = result.data if isinstance(result.data, dict) else {}
                token = data.get("access_token")
                if not isinstance(token, str) or not token:
                    raise ChannelHttpError("QQ getAppAccessToken response carried no access_token", channel=self.name, request_sent=True, category="config_error")
                try:
                    expires_in = int(data.get("expires_in", 7200))  # documented as a number, sent as a string
                except (TypeError, ValueError):
                    expires_in = 7200
                self._access_token = (token, time.time() + expires_in)
            return f"QQBot {self._access_token[0]}"
        # Legacy static bot token (deprecated by QQ in favour of access tokens).
        token = self.bot_token or _require_env(self.cfg.bot_token_env, channel=self.name)
        return f"Bot {app_id}.{token}" if app_id else f"Bot {token}"

    async def send_text(self, recipient: str, text: str, **kwargs) -> dict[str, Any] | None:
        target_type, _, target = recipient.partition(":")
        if not target:
            target_type, target = "group", recipient
        if not target or any(ch in target for ch in "/?#") or any(ch.isspace() for ch in target):
            raise PermissionError("QQ recipient must be group:<openid>, channel:<id> or c2c:<openid>")
        if target_type == "channel":
            path = f"/channels/{target}/messages"
            body: dict[str, Any] = {"content": text}
        elif target_type == "c2c":
            path = f"/v2/users/{target}/messages"
            body = {"content": text, "msg_type": 0}
        elif target_type == "group":
            path = f"/v2/groups/{target}/messages"
            body = {"content": text, "msg_type": 0}
        else:
            raise PermissionError("QQ recipient must be group:<openid>, channel:<id> or c2c:<openid>")
        app_id = self.bot_app_id or os.getenv(self.cfg.bot_app_id_env, "")
        for attempt in range(2):
            headers = {"Authorization": await self._auth_header(refresh=attempt > 0), **_JSON_HEADERS}
            if app_id:
                headers["X-Union-Appid"] = app_id
            try:
                result = await self.http.post(f"{self.api_base}{path}", headers=headers, json=body, idempotency_key=kwargs.get("idempotency_key"), channel=self.name)
            except ChannelHttpError as exc:
                if attempt == 0 and exc.status_code == 401 and self.bot_secret:
                    self._access_token = None  # refused token: nothing was sent
                    continue
                raise
            data = result.data if isinstance(result.data, dict) else {}
            return {"message_id": data.get("id") or data.get("msg_id"), "request_id": result.request_id}
        raise AssertionError("unreachable")  # pragma: no cover
