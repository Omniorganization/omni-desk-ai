"""Teams, QQ and Discord must accept the provider's own signed traffic.

All three used to accept only a static token or a custom OmniDesk HMAC, so
real Bot Connector, QQ and Discord requests were refused (or, for Discord,
needed an undeclared PyNaCl). They must also answer the way the platform
expects: QQ's op 13 validation and op 12 ACK, and a Discord interaction
response within three seconds.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from omnidesk_agent.channels.http_client import ChannelHttpError, ChannelHttpResult
from omnidesk_agent.channels.native_messaging import DiscordChannel, MicrosoftTeamsChannel, QQChannel, qq_signing_key
from omnidesk_agent.config import DiscordConfig, MicrosoftTeamsConfig, QQConfig


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_json(value: dict) -> str:
    return _b64url(json.dumps(value, separators=(",", ":")).encode("utf-8"))


# ----------------------------------------------------------------------------- Teams

APP_ID = "11111111-2222-3333-4444-555555555555"
SERVICE_URL = "https://smba.trafficmanager.net/amer/"


@pytest.fixture(scope="module")
def bf_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(private_key, kid="bf-kid", endorsements=("msteams",)) -> dict:
    numbers = private_key.public_key().public_numbers()
    jwk = {"kty": "RSA", "use": "sig", "kid": kid, "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")), "e": _b64url(numbers.e.to_bytes(3, "big"))}
    if endorsements is not None:
        jwk["endorsements"] = list(endorsements)
    return jwk


def _bf_token(private_key, *, kid="bf-kid", alg="RS256", **claims) -> str:
    now = int(time.time())
    body = {"iss": "https://api.botframework.com", "aud": APP_ID, "exp": now + 600, "nbf": now - 10, "serviceUrl": SERVICE_URL}
    body.update(claims)
    signing_input = f"{_b64url_json({'alg': alg, 'kid': kid, 'typ': 'JWT'})}.{_b64url_json(body)}"
    signature = private_key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


def _teams(monkeypatch, keys: dict, **env) -> MicrosoftTeamsChannel:
    for name in ("MICROSOFT_TEAMS_APP_ID", "MICROSOFT_TEAMS_APP_PASSWORD", "MICROSOFT_TEAMS_TENANT_ID", "MICROSOFT_TEAMS_BOT_TOKEN", "MICROSOFT_TEAMS_INBOUND_TOKEN", "MICROSOFT_TEAMS_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MICROSOFT_TEAMS_APP_ID", APP_ID)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return MicrosoftTeamsChannel(MicrosoftTeamsConfig(service_url=SERVICE_URL), key_fetcher=lambda refresh=False: keys)


ACTIVITY = {"type": "message", "id": "a1", "channelId": "msteams", "serviceUrl": SERVICE_URL, "from": {"id": "29:user"}, "conversation": {"id": "a:conv"}, "text": "hello"}


def test_teams_accepts_a_bot_connector_jwt(monkeypatch, bf_key):
    channel = _teams(monkeypatch, {"bf-kid": _jwk(bf_key)})
    channel.verify_request({"Authorization": "Bearer " + _bf_token(bf_key)}, json.dumps(ACTIVITY).encode(), {}, ACTIVITY)


@pytest.mark.parametrize(
    "token_kwargs, activity_patch, keys_patch",
    [
        ({"iss": "https://sts.windows.net/evil/"}, {}, {}),
        ({"aud": "other-app"}, {}, {}),
        ({"exp": int(time.time()) - 3600}, {}, {}),
        ({"serviceUrl": "https://attacker.example/"}, {}, {}),
        ({}, {"serviceUrl": "https://attacker.example/"}, {}),
        ({}, {"channelId": "webchat"}, {}),  # key endorsed for msteams only
        ({}, {}, {"endorsements": None}),
        ({"kid": "unknown"}, {}, {}),
        ({"alg": "HS256"}, {}, {}),
    ],
)
def test_teams_rejects_tokens_that_do_not_bind_to_this_bot_and_activity(monkeypatch, bf_key, token_kwargs, activity_patch, keys_patch):
    jwk = _jwk(bf_key, endorsements=None) if keys_patch.get("endorsements", ()) is None else _jwk(bf_key)
    channel = _teams(monkeypatch, {"bf-kid": jwk})
    activity = {**ACTIVITY, **activity_patch}
    token = _bf_token(bf_key, **token_kwargs)
    with pytest.raises(PermissionError):
        channel.verify_request({"Authorization": f"Bearer {token}"}, json.dumps(activity).encode(), {}, activity)


def test_teams_with_app_id_refuses_the_legacy_static_bearer(monkeypatch, bf_key):
    channel = _teams(monkeypatch, {"bf-kid": _jwk(bf_key)}, MICROSOFT_TEAMS_INBOUND_TOKEN="static-token")
    with pytest.raises(PermissionError):
        channel.verify_request({"Authorization": "Bearer static-token"}, json.dumps(ACTIVITY).encode(), {}, ACTIVITY)


def test_teams_signature_from_another_key_is_refused(monkeypatch, bf_key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    channel = _teams(monkeypatch, {"bf-kid": _jwk(bf_key)})
    with pytest.raises(PermissionError, match="signature"):
        channel.verify_request({"Authorization": "Bearer " + _bf_token(other)}, json.dumps(ACTIVITY).encode(), {}, ACTIVITY)


def test_teams_send_uses_a_cached_client_credentials_token_and_refreshes_on_401(monkeypatch, bf_key):
    channel = _teams(monkeypatch, {"bf-kid": _jwk(bf_key)}, MICROSOFT_TEAMS_APP_PASSWORD="app-password", MICROSOFT_TEAMS_TENANT_ID="contoso.onmicrosoft.com")
    calls = []
    sends = iter([ChannelHttpError("channel provider HTTP 401", status_code=401, channel="microsoft_teams"), None, None])

    async def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if "login.microsoftonline.com" in url:
            return ChannelHttpResult(200, {"token_type": "Bearer", "expires_in": 3599, "access_token": f"tok-{len(calls)}"})
        outcome = next(sends)
        if outcome is not None:
            raise outcome
        return ChannelHttpResult(201, {"id": "act-1"})

    channel.http.post = fake_post

    async def run_case():
        first = await channel.send_text("a:conv", "hi")
        second = await channel.send_text("a:conv", "again")
        return first, second

    first, second = asyncio.run(run_case())

    token_calls = [kwargs for url, kwargs in calls if "login.microsoftonline.com" in url]
    assert calls[0][0] == "https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/token"
    assert token_calls[0]["data"] == {"grant_type": "client_credentials", "client_id": APP_ID, "client_secret": "app-password", "scope": "https://api.botframework.com/.default"}
    # 401 → one refresh and one resend; the second send reuses the cached token.
    assert len(token_calls) == 2
    activity_calls = [kwargs for url, kwargs in calls if url.endswith("/activities")]
    assert [c["headers"]["Authorization"] for c in activity_calls] == ["Bearer tok-1", "Bearer tok-3", "Bearer tok-3"]
    assert first["message_id"] == second["message_id"] == "act-1"
    with pytest.raises(PermissionError):
        asyncio.run(channel.send_text("a:conv/../../v3/other", "path"))


# ----------------------------------------------------------------------------- QQ

QQ_SECRET = "naOC0ocQE3shWLAfffVLB1rhYPG7"  # 28 chars: the seed repeats it to 32 bytes


def _qq(monkeypatch, **env) -> QQChannel:
    for name in ("QQ_BOT_APP_ID", "QQ_BOT_SECRET", "QQ_BOT_TOKEN", "QQ_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return QQChannel(QQConfig(enabled=True))


def test_qq_seed_repeats_the_secret_to_32_bytes():
    seed = qq_signing_key(QQ_SECRET).private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    # QQ's docs: repeat the secret until it reaches 32 bytes and cut there;
    # their 28-character example ends with its own first four characters.
    assert seed == (QQ_SECRET + "naOC").encode()


def test_qq_op13_validation_is_answered_with_an_ed25519_signature(monkeypatch):
    channel = _qq(monkeypatch, QQ_BOT_SECRET=QQ_SECRET)
    payload = {"op": 13, "d": {"plain_token": "Arq0D5A61EgUu4OxUvOp", "event_ts": "1725442341"}}
    body = json.dumps(payload).encode()
    signed = {"User-Agent": "QQBot-Callback", "X-Signature-Timestamp": "1725442341", "X-Signature-Ed25519": qq_signing_key(QQ_SECRET).sign(b"1725442341" + body).hex()}
    channel.verify_request(signed, body, {}, payload)
    # The handshake itself must be signed by QQ.
    with pytest.raises(PermissionError):
        channel.verify_request({"User-Agent": "QQBot-Callback"}, body, {}, payload)
    answer = channel.webhook_response(channel.parse_webhook(payload), payload)
    assert answer["plain_token"] == "Arq0D5A61EgUu4OxUvOp"
    qq_signing_key(QQ_SECRET).public_key().verify(bytes.fromhex(answer["signature"]), b"1725442341Arq0D5A61EgUu4OxUvOp")


def test_qq_response_is_rebuilt_from_a_verified_payload_without_a_parse_result(monkeypatch):
    # A verified duplicate may skip parsing; QQ still needs the op 13 answer
    # (it re-sends validation after a timeout) and the op 12 ACK for pushes.
    channel = _qq(monkeypatch, QQ_BOT_SECRET=QQ_SECRET)
    op13 = {"op": 13, "d": {"plain_token": "Arq0D5A61EgUu4OxUvOp", "event_ts": "1725442341"}}
    answer = channel.webhook_response(None, op13)
    qq_signing_key(QQ_SECRET).public_key().verify(bytes.fromhex(answer["signature"]), b"1725442341Arq0D5A61EgUu4OxUvOp")
    assert channel.webhook_response(None, {"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {}}) == {"op": 12, "d": 0}
    assert _qq(monkeypatch).webhook_response(None, op13) is None  # no secret: no signing


def test_qq_op13_refuses_values_that_could_forge_an_event_signature(monkeypatch):
    # The validation signs event_ts + plain_token. An event signature covers
    # timestamp + a JSON object body, so a "{" must never be signable here.
    channel = _qq(monkeypatch, QQ_BOT_SECRET=QQ_SECRET)
    for data in ({"plain_token": '{"op":0}', "event_ts": "1"}, {"plain_token": "abc", "event_ts": "1{"}, {"plain_token": "", "event_ts": "1"}, {"plain_token": "a" * 200, "event_ts": "1"}):
        with pytest.raises(PermissionError):
            channel.parse_webhook({"op": 13, "d": data})


def test_qq_event_push_needs_a_valid_signature_and_gets_an_op12_ack(monkeypatch):
    channel = _qq(monkeypatch, QQ_BOT_SECRET=QQ_SECRET)
    payload = {"op": 0, "id": "GROUP_AT_MESSAGE_CREATE:1", "t": "GROUP_AT_MESSAGE_CREATE",
               "d": {"id": "ROBOT1.0_m1", "content": " hello ", "group_openid": "G1", "author": {"member_openid": "M1"}, "timestamp": "2026-11-21T00:00:00+08:00"}}
    body = json.dumps(payload).encode()
    timestamp = "1725442341"
    signature = qq_signing_key(QQ_SECRET).sign(timestamp.encode() + body).hex()

    channel.verify_request({"X-Signature-Ed25519": signature, "X-Signature-Timestamp": timestamp}, body, {}, payload)
    message = channel.parse_webhook(payload)
    assert (message.sender_id, message.thread_id, message.message_id, message.text) == ("M1", "group:G1", "ROBOT1.0_m1", "hello")
    assert channel.webhook_response(message, payload) == {"op": 12, "d": 0}

    forged = Ed25519PrivateKey.generate().sign(timestamp.encode() + body).hex()
    for headers in ({"X-Signature-Ed25519": forged, "X-Signature-Timestamp": timestamp}, {"X-Signature-Timestamp": timestamp}, {"X-Signature-Ed25519": signature, "X-Signature-Timestamp": "1725442342"}):
        with pytest.raises(PermissionError):
            channel.verify_request(headers, body, {}, payload)


def test_qq_c2c_messages_thread_to_the_user(monkeypatch):
    channel = _qq(monkeypatch, QQ_BOT_SECRET=QQ_SECRET)
    message = channel.parse_webhook({"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {"id": "m2", "content": "hi", "author": {"user_openid": "U1"}}})
    assert message.thread_id == "c2c:U1" and message.sender_id == "U1"
    assert channel.parse_webhook({"op": 0, "t": "GUILD_MEMBER_ADD", "d": {"id": "x", "content": "ignored"}}) is None


def test_qq_send_uses_an_app_access_token(monkeypatch):
    channel = _qq(monkeypatch, QQ_BOT_SECRET=QQ_SECRET, QQ_BOT_APP_ID="102000000")
    calls = []

    async def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/getAppAccessToken"):
            return ChannelHttpResult(200, {"access_token": "qq-at", "expires_in": "7200"})
        return ChannelHttpResult(200, {"id": "sent-1", "timestamp": "2026-11-21T00:00:00+08:00"})

    channel.http.post = fake_post
    result = asyncio.run(channel.send_text("group:G1", "hello"))

    assert calls[0][1]["json"] == {"appId": "102000000", "clientSecret": QQ_SECRET}
    assert calls[1][0] == "https://api.bot.qq.com/v2/groups/G1/messages"
    assert calls[1][1]["headers"]["Authorization"] == "QQBot qq-at" and calls[1][1]["headers"]["X-Union-Appid"] == "102000000"
    assert result["message_id"] == "sent-1"
    with pytest.raises(PermissionError):
        asyncio.run(channel.send_text("dms:G1", "unsupported surface"))


# ----------------------------------------------------------------------------- Discord


def _discord(monkeypatch, public_key_hex: str) -> DiscordChannel:
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", public_key_hex)
    monkeypatch.delenv("DISCORD_WEBHOOK_SECRET", raising=False)
    return DiscordChannel(DiscordConfig(enabled=True))


def test_discord_ed25519_is_verified_without_pynacl(monkeypatch):
    key = Ed25519PrivateKey.generate()
    public_hex = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    channel = _discord(monkeypatch, public_hex)
    body = b'{"type":1}'
    timestamp = "1700000000"
    channel.verify_request({"X-Signature-Ed25519": key.sign(timestamp.encode() + body).hex(), "X-Signature-Timestamp": timestamp}, body, {}, {"type": 1})
    # Discord probes endpoints with invalid signatures; they must be refused.
    for headers in ({"X-Signature-Ed25519": "00" * 64, "X-Signature-Timestamp": timestamp}, {"X-Signature-Timestamp": timestamp}, {}):
        with pytest.raises(PermissionError):
            channel.verify_request(headers, body, {}, {"type": 1})


def test_discord_command_gets_an_interaction_response(monkeypatch):
    channel = _discord(monkeypatch, "00" * 32)
    command = {"type": 2, "id": "i1", "channel_id": "c1", "member": {"user": {"id": "u1"}}, "data": {"name": "ask", "options": [{"value": "hi"}]}}
    response = channel.webhook_response(channel.parse_webhook(command), command)
    assert response["type"] == 4 and response["data"]["flags"] == 64
    assert channel.webhook_response(None, {"type": 3}) == {"type": 6}
    assert channel.webhook_response({"type": 1}, {"type": 1}) is None  # PING keeps the route's {"type": 1}


def test_teams_and_qq_failures_before_sending_get_a_plain_outcome(monkeypatch):
    # Same rule as the other adapters: a failed token call is not_sent (not
    # ambiguous), a refused recipient is bad_request, missing setup config_error.
    from omnidesk_agent.channels.provider_errors import classify_channel_error

    teams = _teams(monkeypatch, {}, MICROSOFT_TEAMS_APP_PASSWORD="pw")

    async def timed_out(url, **kwargs):
        raise ChannelHttpError("channel provider network error after the request was sent: timed out", channel="microsoft_teams", request_sent=True, category="timeout")

    teams.http.post = timed_out
    with pytest.raises(ChannelHttpError) as exc:
        asyncio.run(teams.send_text("a:conv", "hi"))
    assert classify_channel_error(exc.value).category == "not_sent"
    with pytest.raises(PermissionError) as refused:
        asyncio.run(teams.send_text("a:conv/x", "hi"))
    assert classify_channel_error(refused.value).category == "bad_request"

    qq = _qq(monkeypatch, QQ_BOT_SECRET=QQ_SECRET)
    with pytest.raises(PermissionError) as refused:
        asyncio.run(qq.send_text("dms:G1", "hi"))
    assert classify_channel_error(refused.value).category == "bad_request"
    with pytest.raises(RuntimeError) as missing:
        asyncio.run(qq.send_text("group:G1", "hi"))  # no QQ_BOT_APP_ID
    assert classify_channel_error(missing.value).category == "config_error"
