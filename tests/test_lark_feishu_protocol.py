"""Lark / Feishu must speak the platform's current event and message protocol.

Real subscriptions send schema 2.0 events (token in ``header.token``), message
content as a JSON *string*, and, with an Encrypt Key, AES-encrypted bodies
signed with X-Lark-Signature. The send API needs ``content`` as a JSON string.
Each of these used to fail against the real platform.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from omnidesk_agent.channels.lark_feishu import FeishuChannel, LarkChannel, decrypt_lark_event, lark_decrypt, lark_signature
from omnidesk_agent.config import FeishuConfig, LarkConfig


def _encrypt(plaintext: bytes, encrypt_key: str) -> str:
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    iv = os.urandom(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv + encryptor.update(padded) + encryptor.finalize()).decode("ascii")


def _event(text: str = "hello", *, token: str = "v-token", sender_type: str = "user", mentions: bool = False) -> dict:
    content_text = f"@_user_1 {text}" if mentions else text
    return {
        "schema": "2.0",
        "header": {"event_id": "ev-1", "event_type": "im.message.receive_v1", "create_time": "1700000000000", "token": token, "app_id": "cli_a", "tenant_key": "t"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_sender", "user_id": "u1"}, "sender_type": sender_type},
            "message": {"message_id": "om_1", "chat_id": "oc_chat", "chat_type": "group", "message_type": "text", "content": json.dumps({"text": content_text}),
                        "mentions": [{"key": "@_user_1", "name": "bot"}] if mentions else []},
        },
    }


@pytest.fixture
def lark(monkeypatch):
    monkeypatch.setenv("LARK_VERIFICATION_TOKEN", "v-token")
    monkeypatch.delenv("LARK_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("LARK_ENCRYPT_KEY", raising=False)
    return LarkChannel(LarkConfig(enabled=True))


def test_documented_decryption_vector():
    # Lark's published example: Encrypt Key "test key" decrypts this to "hello world".
    assert lark_decrypt("P37w+VZImNgPEO1RBhJ6RtKl7n6zymIbEG1pReEzghk=", "test key") == b"hello world"


def test_schema_2_event_is_accepted_and_content_json_is_parsed(lark):
    payload = _event(mentions=True)
    body = json.dumps(payload).encode()

    lark.verify_request({}, body, {}, payload)
    message = lark.parse_webhook(payload)
    envelope = lark.extract_envelope(payload)

    assert message.text == "hello"  # not '{"text":"@_user_1 hello"}'
    assert message.sender_id == "ou_sender" and message.thread_id == "oc_chat" and message.message_id == "om_1"
    assert envelope.message_id == "om_1" and envelope.source_key == "oc_chat"


def test_schema_2_event_with_wrong_header_token_is_refused(lark):
    payload = _event(token="forged")
    with pytest.raises(PermissionError):
        lark.verify_request({}, json.dumps(payload).encode(), {}, payload)
    # A top-level token does not stand in for the schema 2.0 header token.
    payload = {**_event(token="forged"), "token": "v-token"}
    with pytest.raises(PermissionError):
        lark.verify_request({}, json.dumps(payload).encode(), {}, payload)


def test_bot_messages_and_other_events_are_not_turned_into_tasks(lark):
    assert lark.parse_webhook(_event(sender_type="app")) is None
    other = _event()
    other["header"]["event_type"] = "im.message.message_read_v1"
    assert lark.parse_webhook(other) is None


def test_encrypted_signed_event_round_trip(monkeypatch):
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "v-token")
    monkeypatch.setenv("FEISHU_ENCRYPT_KEY", "enc-key")
    monkeypatch.delenv("FEISHU_WEBHOOK_SECRET", raising=False)
    channel = FeishuChannel(FeishuConfig(enabled=True))
    payload = {"encrypt": _encrypt(json.dumps(_event("你好")).encode("utf-8"), "enc-key")}
    body = json.dumps(payload).encode()
    headers = {"X-Lark-Request-Timestamp": "1700000000", "X-Lark-Request-Nonce": "n-1", "X-Lark-Signature": lark_signature("1700000000", "n-1", "enc-key", body)}

    channel.verify_request(headers, body, {}, payload)
    assert channel.parse_webhook(payload).text == "你好"
    assert channel.extract_envelope(payload).message_id == "om_1"

    with pytest.raises(PermissionError):
        channel.verify_request({**headers, "X-Lark-Signature": "0" * 64}, body, {}, payload)
    with pytest.raises(PermissionError):
        channel.verify_request({}, body, {}, payload)  # event pushes must be signed
    with pytest.raises(PermissionError):
        plain = _event()
        channel.verify_request(headers, json.dumps(plain).encode(), {}, plain)  # encryption is on: plaintext refused


def test_encrypted_url_verification_challenge(monkeypatch):
    monkeypatch.setenv("LARK_VERIFICATION_TOKEN", "v-token")
    monkeypatch.setenv("LARK_ENCRYPT_KEY", "enc-key")
    monkeypatch.delenv("LARK_WEBHOOK_SECRET", raising=False)
    channel = LarkChannel(LarkConfig(enabled=True))
    payload = {"encrypt": _encrypt(json.dumps({"challenge": "ch-1", "token": "v-token", "type": "url_verification"}).encode(), "enc-key")}
    channel.verify_request({}, json.dumps(payload).encode(), {}, payload)
    assert channel.parse_webhook(payload) == {"challenge": "ch-1"}
    assert decrypt_lark_event(payload["encrypt"], "enc-key")["type"] == "url_verification"


def test_send_uses_json_string_content_uuid_and_a_cached_token(monkeypatch):
    monkeypatch.setenv("LARK_APP_ID", "cli_a")
    monkeypatch.setenv("LARK_APP_SECRET", "secret")
    channel = LarkChannel(LarkConfig(enabled=True))
    calls = []

    async def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        from omnidesk_agent.channels.http_client import ChannelHttpResult

        if url.endswith("/tenant_access_token/internal"):
            return ChannelHttpResult(200, {"code": 0, "tenant_access_token": "t-1", "expire": 7200})
        return ChannelHttpResult(200, {"code": 0, "data": {"message_id": "om_sent"}}, request_id="log-1")

    channel.http.post = fake_post

    async def run_case():
        first = await channel.send_text("oc_chat", "hi there", idempotency_key="k1")
        await channel.send_text("ou_user", "again", idempotency_key="k2")
        return first

    first = asyncio.run(run_case())

    sends = [kwargs for url, kwargs in calls if url.endswith("/im/v1/messages")]
    assert len([url for url, _ in calls if url.endswith("/internal")]) == 1
    assert sends[0]["params"] == {"receive_id_type": "chat_id"} and sends[1]["params"] == {"receive_id_type": "open_id"}
    assert json.loads(sends[0]["json"]["content"]) == {"text": "hi there"}  # a JSON string, not an object
    assert sends[0]["json"]["uuid"] != sends[1]["json"]["uuid"] and len(sends[0]["json"]["uuid"]) <= 50
    assert sends[0]["idempotent"] is True  # uuid dedupe makes a repeat safe
    assert first == {"message_id": "om_sent", "request_id": "log-1"}


def test_production_lark_accepts_real_signed_traffic_without_the_bridge_header(monkeypatch):
    # Production requires every *_env of an enabled channel, so both the
    # Encrypt Key and the bridge secret are set there. Real Lark pushes carry
    # X-Lark-Signature, never the OmniDesk bridge header, and must still pass;
    # without an Encrypt Key the bridge HMAC stays mandatory once configured.
    from omnidesk_agent.config import AppConfig
    from omnidesk_agent.validation.production import validate_production_config

    cfg = AppConfig()
    cfg.channels.lark.enabled = True
    cfg.channels.lark.allowed_open_ids = ["ou_sender"]
    env = {"OMNIDESK_ENV": "production", "LARK_APP_ID": "a", "LARK_APP_SECRET": "b", "LARK_VERIFICATION_TOKEN": "v-token", "LARK_WEBHOOK_SECRET": "bridge"}
    assert "lark.encrypt_key_env is not configured: LARK_ENCRYPT_KEY" in validate_production_config(cfg, env)["issues"]

    monkeypatch.setenv("LARK_VERIFICATION_TOKEN", "v-token")
    monkeypatch.setenv("LARK_WEBHOOK_SECRET", "bridge")
    monkeypatch.setenv("LARK_ENCRYPT_KEY", "enc-key")
    channel = LarkChannel(LarkConfig(enabled=True))
    payload = {"encrypt": _encrypt(json.dumps(_event()).encode(), "enc-key")}
    body = json.dumps(payload).encode()
    headers = {"X-Lark-Request-Timestamp": "1700000000", "X-Lark-Request-Nonce": "n-1", "X-Lark-Signature": lark_signature("1700000000", "n-1", "enc-key", body)}
    channel.verify_request(headers, body, {}, payload)

    monkeypatch.delenv("LARK_ENCRYPT_KEY")
    plaintext = LarkChannel(LarkConfig(enabled=True))
    plain = _event()
    with pytest.raises(PermissionError):
        plaintext.verify_request({}, json.dumps(plain).encode(), {}, plain)


def test_unauthenticated_ciphertext_is_never_decrypted_and_errors_are_uniform(monkeypatch):
    # AES-CBC decryption of attacker-chosen ciphertext with distinguishable
    # errors is a padding oracle. A signed push is checked before decryption;
    # an unsigned one (only the challenge may be) fails the same way whether
    # it does not decrypt or decrypts to something other than the challenge.
    from omnidesk_agent.channels import lark_feishu

    monkeypatch.setenv("LARK_VERIFICATION_TOKEN", "v-token")
    monkeypatch.setenv("LARK_ENCRYPT_KEY", "enc-key")
    monkeypatch.delenv("LARK_WEBHOOK_SECRET", raising=False)
    channel = LarkChannel(LarkConfig(enabled=True))

    garbage = {"encrypt": base64.b64encode(os.urandom(48)).decode()}
    body = json.dumps(garbage).encode()
    decrypt_calls = []
    real = lark_feishu.decrypt_lark_event
    monkeypatch.setattr(lark_feishu, "decrypt_lark_event", lambda *args: decrypt_calls.append(args) or real(*args))
    wrong_sig = {"X-Lark-Request-Timestamp": "1700000000", "X-Lark-Request-Nonce": "n", "X-Lark-Signature": lark_signature("1700000000", "n", "not-the-key", body)}
    with pytest.raises(PermissionError, match="invalid lark X-Lark-Signature"):
        channel.verify_request(wrong_sig, body, {}, garbage)
    assert decrypt_calls == []  # refused before any decryption

    event = {"encrypt": _encrypt(json.dumps(_event()).encode(), "enc-key")}
    errors = set()
    for payload in (garbage, event):
        with pytest.raises(PermissionError) as exc:
            channel.verify_request({}, json.dumps(payload).encode(), {}, payload)
        errors.add(str(exc.value))
    assert errors == {"missing or invalid lark X-Lark-Signature"}
