from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import time
from typing import Any, Optional, Union

from omnidesk_agent.config import LarkConfig, FeishuConfig
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.channels.http_client import ChannelHttpClient, ChannelHttpError
from omnidesk_agent.channels.idempotency import provider_retry_key
from omnidesk_agent.channels.provider_errors import ensure_provider_success

# Lark answers an invalid or expired tenant_access_token with these codes; the
# message was not sent, so it may be repeated once with a fresh token.
LARK_TOKEN_REJECTED_CODES = {"99991661", "99991663", "99991668"}
_RECEIVE_ID_TYPES = {"open_id", "user_id", "union_id", "email", "chat_id"}
_MENTION_KEY_RE = re.compile(r"@_user_\d+")


def lark_decrypt(encrypted: str, encrypt_key: str) -> bytes:
    """AES-256-CBC with key = SHA-256(Encrypt Key) and the IV in the first 16 bytes."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    try:
        raw = base64.b64decode(encrypted, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PermissionError("malformed Lark encrypted event") from exc
    if len(raw) < 32 or len(raw) % 16:
        raise PermissionError("malformed Lark encrypted event")
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    decryptor = Cipher(algorithms.AES(key), modes.CBC(raw[:16])).decryptor()
    padded = decryptor.update(raw[16:]) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    try:
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise PermissionError("Lark encrypted event could not be decrypted with the configured Encrypt Key") from exc


def decrypt_lark_event(encrypted: str, encrypt_key: str) -> dict[str, Any]:
    """Decrypt an ``{"encrypt": ...}`` event body into the event object."""
    try:
        value = json.loads(lark_decrypt(encrypted, encrypt_key).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PermissionError("Lark encrypted event could not be decrypted with the configured Encrypt Key") from exc
    if not isinstance(value, dict):
        raise PermissionError("malformed Lark encrypted event")
    return value


def lark_signature(timestamp: str, nonce: str, encrypt_key: str, body: bytes) -> str:
    """X-Lark-Signature: hex SHA-256 over timestamp + nonce + Encrypt Key + raw body."""
    return hashlib.sha256(timestamp.encode("utf-8") + nonce.encode("utf-8") + encrypt_key.encode("utf-8") + body).hexdigest()


def _text_from_content(message: dict[str, Any]) -> str:
    """Plain text of an im.message.receive_v1 message; ``content`` is a JSON string."""
    content = message.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return ""
    if not isinstance(content, dict):
        return ""
    message_type = message.get("message_type") or message.get("msg_type") or "text"
    if message_type == "text":
        text = content.get("text")
        text = text if isinstance(text, str) else ""
    elif message_type == "post":
        body = content
        for locale in ("zh_cn", "en_us", "ja_jp"):
            if isinstance(content.get(locale), dict):
                body = content[locale]
                break
        parts = [str(body.get("title") or "")]
        for line in body.get("content") or []:
            if isinstance(line, list):
                parts.extend(str(node.get("text") or "") for node in line if isinstance(node, dict) and node.get("tag") in {"text", "a"})
        text = " ".join(part for part in parts if part)
    else:
        return ""
    return _MENTION_KEY_RE.sub("", text).strip()


class _BaseLarkFeishuChannel:
    """Lark / Feishu Open Platform bot adapter (event subscription + im/v1/messages).

    Inbound accepts schema 2.0 (``header.token``) and legacy 1.0 events. With an
    Encrypt Key configured, bodies are decrypted and ``X-Lark-Signature`` is
    verified; without one, the verification token in the event is the only
    provider authentication, as the platform defines it.
    """

    name = "lark"
    api_base = "https://open.larksuite.com/open-apis"

    def __init__(self, cfg: Union[LarkConfig, FeishuConfig]):
        self.cfg = cfg
        self.app_id = os.getenv(cfg.app_id_env, "")
        self.app_secret = os.getenv(cfg.app_secret_env, "")
        self.verification_token = os.getenv(cfg.verification_token_env, "")
        self.encrypt_key = os.getenv(getattr(cfg, "encrypt_key_env", ""), "")
        self._tenant_token: Optional[tuple[str, float]] = None
        self.http = ChannelHttpClient()

    def _decoded(self, payload: Any) -> Any:
        if isinstance(payload, dict) and isinstance(payload.get("encrypt"), str):
            if not self.encrypt_key:
                raise PermissionError(f"{self.name} event is encrypted but no Encrypt Key is configured")
            return decrypt_lark_event(payload["encrypt"], self.encrypt_key)
        return payload

    @staticmethod
    def _event_token(payload: dict[str, Any]) -> Any:
        header = payload.get("header")
        if payload.get("schema") == "2.0" and isinstance(header, dict):
            return header.get("token")
        return payload.get("token")

    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload) -> None:
        from omnidesk_agent.channels.verify import env_secret, header, verify_hmac_sha256

        token = self.verification_token or env_secret(self.cfg.verification_token_env, channel=self.name)
        encrypted = isinstance(payload, dict) and isinstance(payload.get("encrypt"), str)
        if self.encrypt_key and not encrypted:
            raise PermissionError(f"{self.name} Encrypt Key is configured but the event is not encrypted")
        if self.encrypt_key:
            signature = header(headers, "x-lark-signature")
            if signature:
                # The signature covers the raw (encrypted) body, so it is checked
                # before anything is decrypted: unauthenticated ciphertext never
                # reaches the AES-CBC decryptor.
                timestamp = header(headers, "x-lark-request-timestamp")
                nonce = header(headers, "x-lark-request-nonce")
                if not timestamp or not nonce or not hmac.compare_digest(signature.lower(), lark_signature(timestamp, nonce, self.encrypt_key, body)):
                    raise PermissionError(f"invalid {self.name} X-Lark-Signature")
                decoded = self._decoded(payload)
            else:
                # Only the URL-verification challenge is unsigned. Every failure
                # here gives the same answer, so a padding or format error is not
                # distinguishable from a missing signature (no decryption oracle).
                try:
                    decoded = self._decoded(payload)
                except PermissionError:
                    decoded = None
                if not isinstance(decoded, dict) or decoded.get("type") != "url_verification":
                    raise PermissionError(f"missing or invalid {self.name} X-Lark-Signature")
        else:
            decoded = payload
        if not isinstance(decoded, dict):
            raise PermissionError(f"invalid {self.name} event body")
        supplied = self._event_token(decoded)
        if not token or not isinstance(supplied, str) or not hmac.compare_digest(supplied, token):
            raise PermissionError(f"invalid {self.name} verification token")
        if self.encrypt_key:
            # X-Lark-Signature already covers the body. Lark never sends the
            # OmniDesk bridge header, so demanding it here (production sets
            # every *_env, the bridge secret included) would refuse real traffic.
            return
        # Plaintext mode: the static verification token is the platform's only
        # check; a deployment with a body-signing relay requires its HMAC too.
        secret = os.getenv(getattr(self.cfg, "webhook_secret_env", ""), "")
        bridge_signature = header(headers, "x-omnidesk-webhook-signature-256")
        if secret or bridge_signature:
            verify_hmac_sha256(body, secret or env_secret(self.cfg.webhook_secret_env, channel=self.name), bridge_signature, prefix="sha256=")

    def extract_envelope(self, payload: dict[str, Any]):
        from omnidesk_agent.channels.base import WebhookEnvelope

        try:
            payload = self._decoded(payload)
        except PermissionError:
            return WebhookEnvelope(raw={})
        if not isinstance(payload, dict):
            return WebhookEnvelope(raw={})
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        sid = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
        sender_id = str(sid.get("open_id") or sid.get("user_id") or event.get("open_id") or "unknown")
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        mid = str(message.get("message_id") or header.get("event_id") or payload.get("uuid") or "")
        source = str(message.get("chat_id") or event.get("open_chat_id") or sender_id)
        return WebhookEnvelope(source_key=source, sender_id=sender_id, message_id=mid or None, event_type=str(header.get("event_type") or payload.get("type") or "") or None, raw=payload)

    def parse_webhook(self, payload: dict[str, Any]) -> Optional[Union[ChannelMessage, dict[str, str]]]:
        payload = self._decoded(payload)
        if not isinstance(payload, dict):
            return None
        if payload.get("type") == "url_verification":
            if not self.verification_token or not isinstance(payload.get("token"), str) or not hmac.compare_digest(payload["token"], self.verification_token):
                return None
            return {"challenge": str(payload.get("challenge", ""))}
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        if payload.get("schema") == "2.0":
            if header.get("event_type") != "im.message.receive_v1":
                return None
            sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
            if sender.get("sender_type") not in (None, "user"):
                return None  # never react to bot/app messages (loops)
            sid = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
            sender_id = str(sid.get("open_id") or sid.get("user_id") or "unknown")
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            text = _text_from_content(message)
            thread_id = str(message.get("chat_id") or sender_id)
            message_id = str(message.get("message_id") or "")
        else:
            # Legacy 1.0 event_callback with event.type == "message".
            if event.get("type") not in (None, "message"):
                return None
            sender_id = str(event.get("open_id") or "unknown")
            text = str(event.get("text_without_at_bot") or event.get("text") or "").strip()
            thread_id = str(event.get("open_chat_id") or sender_id)
            message_id = str(event.get("open_message_id") or payload.get("uuid") or "")
        if self.cfg.allowed_open_ids and sender_id not in self.cfg.allowed_open_ids:
            return None
        if not text:
            return None
        return ChannelMessage(channel=self.name, sender_id=sender_id, thread_id=thread_id, message_id=message_id, text=text, raw=payload)

    async def _tenant_access_token(self, *, refresh: bool = False) -> str:
        if not refresh and self._tenant_token and self._tenant_token[1] > time.time() + 60:
            return self._tenant_token[0]
        if not self.app_id or not self.app_secret:
            raise RuntimeError(f"{self.name} app id/secret missing")
        result = await self.http.post(
            f"{self.api_base}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            channel=self.name,
            idempotent=True,
        )
        ensure_provider_success(self.name, result)
        data = result.data if isinstance(result.data, dict) else {}
        token = data.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise ChannelHttpError(f"{self.name} tenant_access_token missing from response", channel=self.name, request_sent=True, category="config_error")
        try:
            expire = int(data.get("expire", 7200))
        except (TypeError, ValueError):
            expire = 7200
        self._tenant_token = (token, time.time() + expire)
        return token

    @staticmethod
    def _receive_id_type(recipient: str) -> str:
        if recipient.startswith("oc_"):
            return "chat_id"
        if recipient.startswith("on_"):
            return "union_id"
        if recipient.startswith("ou_"):
            return "open_id"
        if "@" in recipient:
            return "email"
        return "open_id"

    async def send_text(self, recipient: str, text: str, **kwargs) -> dict[str, Any]:
        receive_id_type = self._receive_id_type(str(recipient))
        if receive_id_type not in _RECEIVE_ID_TYPES:  # pragma: no cover - defensive
            raise ValueError("unsupported Lark receive_id_type")
        idempotency_key = kwargs.get("idempotency_key")
        body: dict[str, Any] = {"receive_id": recipient, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}
        if idempotency_key:
            # Lark sends at most one message per uuid within an hour, so a
            # repeated request cannot post twice.
            body["uuid"] = provider_retry_key(str(idempotency_key))
        for attempt in range(2):
            token = await self._tenant_access_token(refresh=attempt > 0)
            try:
                result = await self.http.post(
                    f"{self.api_base}/im/v1/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"receive_id_type": receive_id_type},
                    json=body,
                    idempotency_key=idempotency_key,
                    channel=self.name,
                    idempotent=bool(idempotency_key),
                )
                ensure_provider_success(self.name, result)
            except ChannelHttpError as exc:
                code = exc.provider_code or (str(exc.response_json.get("code")) if isinstance(exc.response_json, dict) and exc.response_json.get("code") is not None else None)
                if attempt == 0 and code in LARK_TOKEN_REJECTED_CODES:
                    self._tenant_token = None
                    continue
                raise
            data = result.data.get("data") if isinstance(result.data, dict) and isinstance(result.data.get("data"), dict) else {}
            return {"message_id": data.get("message_id"), "request_id": result.request_id}
        raise AssertionError("unreachable")  # pragma: no cover


class LarkChannel(_BaseLarkFeishuChannel):
    name = "lark"
    api_base = "https://open.larksuite.com/open-apis"


class FeishuChannel(_BaseLarkFeishuChannel):
    name = "feishu"
    api_base = "https://open.feishu.cn/open-apis"
