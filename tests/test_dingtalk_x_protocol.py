"""DingTalk and X must accept traffic signed the way the real provider signs it.

Both adapters used to accept only a custom OmniDesk HMAC header that no
provider sends, so real callbacks were refused unless an unshipped relay
re-signed them. Outbound, the approved recipient must be where the message
goes, and X posts need user-context (OAuth 1.0a) credentials.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time

import pytest

from omnidesk_agent.channels.dingtalk import DingTalkChannel, dingtalk_sign
from omnidesk_agent.channels.http_client import ChannelHttpResult
from omnidesk_agent.channels.x_channel import XChannel, oauth1_authorization, x_signature
from omnidesk_agent.config import DingTalkConfig, XConfig


# ----------------------------------------------------------------------------- DingTalk


def _dingtalk(monkeypatch, **env) -> DingTalkChannel:
    for name in ("DINGTALK_APP_KEY", "DINGTALK_APP_SECRET", "DINGTALK_WEBHOOK_SECRET", "DINGTALK_ROBOT_TOKEN", "DINGTALK_ROBOT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return DingTalkChannel(DingTalkConfig(enabled=True))


def _signed_headers(secret: str, at: float | None = None) -> dict[str, str]:
    timestamp = str(int((at if at is not None else time.time()) * 1000))
    return {"timestamp": timestamp, "sign": dingtalk_sign(timestamp, secret)}


def test_dingtalk_sign_matches_the_documented_formula():
    expected = base64.b64encode(hmac.new(b"app-secret", b"1700000000000\napp-secret", hashlib.sha256).digest()).decode()
    assert dingtalk_sign("1700000000000", "app-secret") == expected


def test_dingtalk_callback_signed_with_app_secret_is_accepted_once(monkeypatch):
    channel = _dingtalk(monkeypatch, DINGTALK_APP_SECRET="app-secret")
    headers = _signed_headers("app-secret")
    body = json.dumps({"msgId": "m1", "conversationId": "cid1", "text": {"content": "hi"}}).encode()

    channel.verify_request(headers, body, {}, json.loads(body))
    # The sign covers only the timestamp, so a captured one is single-use.
    with pytest.raises(PermissionError, match="replayed"):
        channel.verify_request(headers, b'{"forged":true}', {}, {"forged": True})


def test_dingtalk_rejects_wrong_stale_or_missing_signatures(monkeypatch):
    channel = _dingtalk(monkeypatch, DINGTALK_APP_SECRET="app-secret")
    with pytest.raises(PermissionError, match="invalid DingTalk"):
        channel.verify_request(_signed_headers("other-secret"), b"{}", {}, {})
    with pytest.raises(PermissionError, match="stale"):
        channel.verify_request(_signed_headers("app-secret", at=time.time() - 3600), b"{}", {}, {})
    with pytest.raises(PermissionError, match="invalid DingTalk timestamp"):
        channel.verify_request({"timestamp": "+1700000000000", "sign": "x"}, b"{}", {}, {})
    with pytest.raises(PermissionError, match="missing"):
        channel.verify_request({}, b"{}", {}, {})
    # Without the app secret configured nothing is accepted.
    unconfigured = _dingtalk(monkeypatch)
    with pytest.raises(PermissionError):
        unconfigured.verify_request(_signed_headers("app-secret"), b"{}", {}, {})


def test_dingtalk_reply_goes_to_the_conversation_it_names(monkeypatch):
    channel = _dingtalk(monkeypatch, DINGTALK_APP_KEY="ding-app", DINGTALK_APP_SECRET="app-secret", DINGTALK_ROBOT_TOKEN="robot-token")
    calls = []

    async def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/oauth2/accessToken"):
            return ChannelHttpResult(200, {"accessToken": "at-1", "expireIn": 7200})
        if url.endswith("/oToMessages/batchSend"):
            return ChannelHttpResult(200, {"processQueryKey": "pq-user", "invalidStaffIdList": [], "flowControlledStaffIdList": []})
        if "oapi.dingtalk.com/robot/send" in url:
            return ChannelHttpResult(200, {"errcode": 0, "errmsg": "ok"})
        return ChannelHttpResult(200, {"processQueryKey": "pq-group"})

    channel.http.post = fake_post

    async def run_case():
        group = await channel.send_text("cidGROUP==", "to group")
        user = await channel.send_text("user:staff-1", "to user")
        robot = await channel.send_text("robot", "to robot group")
        return group, user, robot

    group, user, robot = asyncio.run(run_case())

    group_call = next(kwargs for url, kwargs in calls if url.endswith("/groupMessages/send"))
    user_call = next(kwargs for url, kwargs in calls if url.endswith("/oToMessages/batchSend"))
    assert group_call["json"]["openConversationId"] == "cidGROUP==" and group_call["json"]["robotCode"] == "ding-app"
    assert json.loads(group_call["json"]["msgParam"]) == {"content": "to group"}
    assert user_call["json"]["userIds"] == ["staff-1"]
    assert group_call["headers"]["x-acs-dingtalk-access-token"] == "at-1"
    assert (group["message_id"], user["message_id"]) == ("pq-group", "pq-user")
    assert robot["message_id"] is None


def test_dingtalk_refuses_a_recipient_it_cannot_honour(monkeypatch):
    # The approver saw the recipient; the group robot cannot DM anyone, so a
    # recipient it would silently ignore is refused instead.
    channel = _dingtalk(monkeypatch, DINGTALK_ROBOT_TOKEN="robot-token")

    async def fake_post(url, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("no request expected")

    channel.http.post = fake_post
    for recipient in ("staff-1", "alice", "", "dm:1"):
        with pytest.raises(PermissionError):
            asyncio.run(channel.send_text(recipient, "hi"))


def test_dingtalk_user_send_reports_rejected_user(monkeypatch):
    channel = _dingtalk(monkeypatch, DINGTALK_APP_KEY="ding-app", DINGTALK_APP_SECRET="app-secret")

    async def fake_post(url, **kwargs):
        if url.endswith("/oauth2/accessToken"):
            return ChannelHttpResult(200, {"accessToken": "at-1", "expireIn": 7200})
        return ChannelHttpResult(200, {"processQueryKey": "pq", "invalidStaffIdList": ["staff-1"], "flowControlledStaffIdList": []})

    channel.http.post = fake_post
    from omnidesk_agent.channels.http_client import ChannelHttpError

    with pytest.raises(ChannelHttpError) as exc:
        asyncio.run(channel.send_text("user:staff-1", "hi"))
    assert exc.value.category == "bad_request"


def test_dingtalk_one_to_one_message_replies_to_the_user():
    channel = DingTalkChannel(DingTalkConfig(enabled=True))
    message = channel.parse_webhook({"msgId": "m1", "conversationId": "cidP2P", "conversationType": "1", "senderStaffId": "staff-9", "text": {"content": " hi "}})
    assert message.thread_id == "user:staff-9" and message.text == "hi"
    group = channel.parse_webhook({"msgId": "m2", "conversationId": "cidG", "conversationType": "2", "senderStaffId": "staff-9", "text": {"content": "hi"}})
    assert group.thread_id == "cidG"


# ----------------------------------------------------------------------------- X


def _x(monkeypatch, **env) -> XChannel:
    for name in ("X_BEARER_TOKEN", "X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET", "X_WEBHOOK_CRC_TOKEN", "X_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return XChannel(XConfig(enabled=True))


DM_EVENT = {
    "for_user_id": "100",
    "direct_message_events": [{"type": "message_create", "id": "dm1", "created_timestamp": "1700000000000",
                               "message_create": {"target": {"recipient_id": "100"}, "sender_id": "200", "message_data": {"text": "hello"}}}],
}


def test_x_event_signed_with_consumer_secret_is_accepted(monkeypatch):
    channel = _x(monkeypatch, X_WEBHOOK_CRC_TOKEN="consumer-secret")
    body = json.dumps(DM_EVENT).encode()
    signature = "sha256=" + base64.b64encode(hmac.new(b"consumer-secret", body, hashlib.sha256).digest()).decode()

    channel.verify_request({"x-twitter-webhooks-signature": signature}, body, {}, DM_EVENT)
    messages = channel.parse_webhook(DM_EVENT)
    assert [(m.sender_id, m.thread_id, m.text) for m in messages] == [("200", "dm:200", "hello")]

    with pytest.raises(PermissionError):
        channel.verify_request({"x-twitter-webhooks-signature": x_signature("other", body)}, body, {}, DM_EVENT)
    with pytest.raises(PermissionError):
        channel.verify_request({"x-twitter-webhooks-signature": signature}, b'{"tampered":1}', {}, {"tampered": 1})


def test_x_oauth2_signature_header_is_verified(monkeypatch):
    # Apps on OAuth 2.0 get X-Twitter-Webhooks-Signature-OAuth2 (client secret).
    channel = _x(monkeypatch, X_WEBHOOK_CRC_TOKEN="oauth2-client-secret")
    body = json.dumps(DM_EVENT).encode()
    both = {"x-twitter-webhooks-signature-oauth2": x_signature("oauth2-client-secret", body), "x-twitter-webhooks-signature": x_signature("consumer-secret-we-do-not-hold", body)}
    channel.verify_request(both, body, {}, DM_EVENT)
    with pytest.raises(PermissionError):
        channel.verify_request({"x-twitter-webhooks-signature-oauth2": x_signature("guess", body)}, body, {}, DM_EVENT)


def test_x_crc_and_event_signature_share_the_format(monkeypatch):
    channel = _x(monkeypatch, X_WEBHOOK_CRC_TOKEN="consumer-secret")
    assert channel.crc_response("abc")["response_token"] == x_signature("consumer-secret", b"abc")


def test_x_ignores_echo_of_our_own_dm(monkeypatch):
    channel = _x(monkeypatch)
    echo = {"for_user_id": "100", "direct_message_events": [{"type": "message_create", "id": "dm2", "message_create": {"sender_id": "100", "message_data": {"text": "our reply"}}}]}
    assert channel.parse_webhook(echo) == []


def test_x_non_object_body_is_refused(monkeypatch):
    channel = _x(monkeypatch, X_WEBHOOK_CRC_TOKEN="consumer-secret")
    with pytest.raises(PermissionError):
        channel.verify_request({"x-twitter-webhooks-signature": x_signature("consumer-secret", b"123")}, b"123", {}, 123)


def test_oauth1_signature_matches_x_documented_example():
    header = oauth1_authorization(
        "POST", "https://api.twitter.com/1.1/statuses/update.json?include_entities=true",
        consumer_key="xvz1evFS4wEEPTGEFPHBog", consumer_secret="kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
        token="370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb", token_secret="LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
        extra_params={"status": "Hello Ladies + Gentlemen, a signed OAuth request!"},
        nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg", timestamp="1318622958",
    )
    assert 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in header


def test_x_posts_and_dms_with_user_context(monkeypatch):
    channel = _x(monkeypatch, X_API_KEY="ck", X_API_SECRET="cs", X_ACCESS_TOKEN="at", X_ACCESS_SECRET="as", X_BEARER_TOKEN="app-only")
    calls = []

    async def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if "/dm_conversations/" in url:
            return ChannelHttpResult(201, {"data": {"dm_conversation_id": "100-200", "dm_event_id": "dme-1"}})
        return ChannelHttpResult(201, {"data": {"id": "tw-1", "text": "hi"}})

    channel.http.post = fake_post

    async def run_case():
        return await channel.send_text("public", "hi"), await channel.send_text("dm:200", "hello back")

    post, dm = asyncio.run(run_case())

    assert calls[0][0] == "https://api.x.com/2/tweets" and calls[1][0] == "https://api.x.com/2/dm_conversations/with/200/messages"
    assert all(kwargs["headers"]["Authorization"].startswith("OAuth ") for _, kwargs in calls)  # never the app-only bearer
    assert (post["message_id"], dm["message_id"]) == ("tw-1", "dme-1")
    with pytest.raises(NotImplementedError):
        asyncio.run(channel.send_text("200", "ambiguous recipient"))
    with pytest.raises(ValueError):
        asyncio.run(channel.send_text("dm:../../2/tweets", "path injection"))
