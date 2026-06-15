from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Optional
from uuid import uuid4

from omnidesk_agent.channels.base import WebhookEnvelope
from omnidesk_agent.channels.http_client import ChannelHttpClient
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
        data = result.data or {}
        if data and data.get("ok") is False:
            raise RuntimeError(f"Slack API error: {data.get('error')}")
        return {"message_id": data.get("ts"), "request_id": result.request_id}


class DiscordChannel:
    """Discord interactions/webhook-compatible adapter.

    Discord bot Gateway delivery is normally websocket-based. For production
    ingress, deploy an edge bridge that verifies Discord's Ed25519 interaction
    signature and forwards the normalized JSON through this signed webhook route,
    or install PyNaCl and use the optional public-key verification here.
    """

    name = "discord"

    def __init__(self, cfg: DiscordConfig):
        self.cfg = cfg
        self.bot_token = os.getenv(cfg.bot_token_env, "")
        self.webhook_secret = os.getenv(cfg.webhook_secret_env, "")
        self.public_key = os.getenv(cfg.public_key_env, "")
        self.http = ChannelHttpClient()

    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload: Any) -> None:
        signature = header(headers, "x-signature-ed25519")
        timestamp = header(headers, "x-signature-timestamp")
        if self.public_key and signature and timestamp:
            try:
                from nacl.signing import VerifyKey  # type: ignore[import-not-found]
                from nacl.exceptions import BadSignatureError  # type: ignore[import-not-found]
            except ModuleNotFoundError as exc:
                raise PermissionError("PyNaCl is required for Discord Ed25519 verification") from exc
            try:
                VerifyKey(bytes.fromhex(self.public_key)).verify(timestamp.encode("utf-8") + body, bytes.fromhex(signature))
            except BadSignatureError as exc:  # type: ignore[misc]
                raise PermissionError("invalid Discord Ed25519 signature") from exc
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

    async def send_text(self, recipient: str, text: str, **kwargs) -> dict[str, Any] | None:
        token = self.bot_token or _require_env(self.cfg.bot_token_env, channel=self.name)
        url = f"https://discord.com/api/v10/channels/{recipient}/messages"
        result = await self.http.post(url, headers={"Authorization": f"Bot {token}", **_JSON_HEADERS}, json={"content": text}, idempotency_key=kwargs.get("idempotency_key"), channel=self.name)
        data = result.data or {}
        return {"message_id": data.get("id"), "request_id": result.request_id}


class GoogleChatChannel:
    name = "google_chat"

    def __init__(self, cfg: GoogleChatConfig):
        self.cfg = cfg
        self.webhook_secret = os.getenv(cfg.webhook_secret_env, "")
        self.incoming_webhook_url = os.getenv(cfg.incoming_webhook_url_env, "")
        self.http = ChannelHttpClient()

    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload: Any) -> None:
        token = header(headers, "x-goog-channel-token") or query_params.get("token", "")
        expected = os.getenv(self.cfg.channel_token_env, "")
        if expected and token:
            if not hmac.compare_digest(token, expected):
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

    async def send_text(self, recipient: str, text: str, **kwargs) -> dict[str, Any] | None:
        url = kwargs.get("webhook_url") or self.incoming_webhook_url or recipient
        if not str(url).startswith("https://"):
            raise RuntimeError("Google Chat incoming webhook URL is missing")
        result = await self.http.post(str(url), json={"text": text}, idempotency_key=kwargs.get("idempotency_key"), channel=self.name)
        data = result.data or {}
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
    name = "microsoft_teams"

    def __init__(self, cfg: MicrosoftTeamsConfig):
        self.cfg = cfg
        self.bot_token = os.getenv(cfg.bot_token_env, "")
        self.webhook_secret = os.getenv(cfg.webhook_secret_env, "")
        self.http = ChannelHttpClient()

    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload: Any) -> None:
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

    async def send_text(self, recipient: str, text: str, **kwargs) -> dict[str, Any] | None:
        token = self.bot_token or _require_env(self.cfg.bot_token_env, channel=self.name)
        service_url = kwargs.get("service_url") or self.cfg.service_url
        if not service_url:
            raise RuntimeError("Teams service_url is missing")
        url = f"{str(service_url).rstrip('/')}/v3/conversations/{recipient}/activities"
        body = {"type": "message", "text": text}
        result = await self.http.post(url, headers={"Authorization": f"Bearer {token}", **_JSON_HEADERS}, json=body, idempotency_key=kwargs.get("idempotency_key"), channel=self.name)
        data = result.data or {}
        return {"message_id": data.get("id"), "request_id": result.request_id}


class MatrixChannel:
    name = "matrix"

    def __init__(self, cfg: MatrixConfig):
        self.cfg = cfg
        self.homeserver_url = cfg.homeserver_url.rstrip("/") if cfg.homeserver_url else ""
        self.access_token = os.getenv(cfg.access_token_env, "")
        self.webhook_secret = os.getenv(cfg.webhook_secret_env, "")
        self.http = ChannelHttpClient()

    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload: Any) -> None:
        token = header(headers, "x-matrix-hook-token") or query_params.get("token", "")
        expected = os.getenv(self.cfg.webhook_token_env, "")
        if expected and token:
            if not hmac.compare_digest(token, expected):
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


class QQChannel:
    """QQ Bot OpenAPI adapter.

    Recipient prefixes select the outbound surface:
    - group:<group_openid> sends to QQ group OpenAPI
    - channel:<channel_id> sends to guild/channel messages
    - c2c:<openid> sends to C2C OpenAPI
    """

    name = "qq"

    def __init__(self, cfg: QQConfig):
        self.cfg = cfg
        self.bot_app_id = os.getenv(cfg.bot_app_id_env, "")
        self.bot_token = os.getenv(cfg.bot_token_env, "")
        self.webhook_secret = os.getenv(cfg.webhook_secret_env, "")
        self.api_base = cfg.api_base.rstrip("/")
        self.http = ChannelHttpClient()

    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload: Any) -> None:
        verify_hmac_sha256(body, self.webhook_secret or env_secret(self.cfg.webhook_secret_env, channel=self.name), header(headers, "x-omnidesk-webhook-signature-256"), prefix="sha256=")

    def extract_envelope(self, payload: dict[str, Any]) -> WebhookEnvelope:
        data = payload.get("d") or payload.get("data") or payload
        author = data.get("author") or data.get("member") or {}
        sender = str(author.get("id") or author.get("user_id") or data.get("openid") or "unknown")
        source = str(data.get("group_openid") or data.get("channel_id") or data.get("guild_id") or sender)
        mid = str(data.get("id") or data.get("msg_id") or payload.get("id") or "")
        return WebhookEnvelope(source_key=source, sender_id=sender, message_id=mid or None, raw=payload)

    def parse_webhook(self, payload: dict[str, Any]) -> Optional[ChannelMessage | dict[str, Any]]:
        if payload.get("op") == 13:
            return {"ok": True, "ignored": True, "event": "qq_callback_ack"}
        data = payload.get("d") or payload.get("data") or payload
        author = data.get("author") or data.get("member") or {}
        sender = str(author.get("id") or author.get("user_id") or data.get("openid") or "")
        source = str(data.get("group_openid") or data.get("channel_id") or data.get("guild_id") or sender)
        if self.cfg.allowed_user_ids and sender not in self.cfg.allowed_user_ids:
            return None
        if self.cfg.allowed_source_ids and source not in self.cfg.allowed_source_ids:
            return None
        text = str(data.get("content") or data.get("text") or "").strip()
        if not text:
            return None
        return ChannelMessage(channel=self.name, sender_id=sender or "unknown", thread_id=source or sender, message_id=str(data.get("id") or data.get("msg_id") or ""), text=text, raw=payload)

    def _auth_header(self) -> str:
        token = self.bot_token or _require_env(self.cfg.bot_token_env, channel=self.name)
        app_id = self.bot_app_id or os.getenv(self.cfg.bot_app_id_env, "")
        return f"QQBot {token}" if not app_id else f"QQBot {app_id}.{token}"

    async def send_text(self, recipient: str, text: str, **kwargs) -> dict[str, Any] | None:
        target_type, _, target = recipient.partition(":")
        if not target:
            target_type, target = "group", recipient
        if target_type == "channel":
            path = f"/channels/{target}/messages"
            body = {"content": text}
        elif target_type == "c2c":
            path = f"/v2/users/{target}/messages"
            body = {"content": text, "msg_type": 0}
        else:
            path = f"/v2/groups/{target}/messages"
            body = {"content": text, "msg_type": 0}
        result = await self.http.post(f"{self.api_base}{path}", headers={"Authorization": self._auth_header(), **_JSON_HEADERS}, json=body, idempotency_key=kwargs.get("idempotency_key"), channel=self.name)
        data = result.data or {}
        return {"message_id": data.get("id") or data.get("msg_id"), "request_id": result.request_id}
