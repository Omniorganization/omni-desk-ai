from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnidesk_agent.appsync import push
from omnidesk_agent.appsync.push import FirebasePushProvider, normalize_push_target, split_push_target

FID = "cX1yZ2abcdefghijklmnop"  # 22 base64url characters
LEGACY_TOKEN = "dXyZ1234567890abcdefgh:APA91bLegacyRegistrationToken"


def test_push_targets_are_validated_and_typed():
    assert normalize_push_target(FID, "fid") == f"fid:{FID}"
    assert normalize_push_target(LEGACY_TOKEN) == LEGACY_TOKEN
    assert normalize_push_target("", "fid") is None
    assert split_push_target(f"fid:{FID}") == ("fid", FID)
    assert split_push_target(LEGACY_TOKEN) == ("fcm_token", LEGACY_TOKEN)
    for bad, kind in (("short", "fid"), (FID + "!", "fid"), (f"fid:{FID}", "fcm_token"), (FID, "apns")):
        with pytest.raises(ValueError):
            normalize_push_target(bad, kind)


class _Message:
    """firebase_admin Message without the `fid` field (older SDKs)."""

    def __init__(self, *, token=None, notification=None, data=None):
        self.token = token
        self.notification = notification
        self.data = data


class _MessageWithFid(_Message):
    def __init__(self, *, fid=None, **kwargs):
        super().__init__(**kwargs)
        self.fid = fid


def _messaging(message_cls):
    sent = []

    def send(message, app=None):
        sent.append(message)
        return "projects/p/messages/1"

    module = SimpleNamespace(Message=message_cls, Notification=lambda title, body: (title, body), send=send)
    return module, sent


def _provider(monkeypatch, message_cls):
    module, sent = _messaging(message_cls)
    provider = FirebasePushProvider(app=object())
    monkeypatch.setattr(provider, "_sdk_available", lambda: True)
    monkeypatch.setattr(provider, "_messaging", lambda: module)
    return provider, sent


def test_fid_targets_use_the_fid_field(monkeypatch):
    provider, sent = _provider(monkeypatch, _MessageWithFid)
    result = provider.send({"push_id": "p1", "push_token": f"fid:{FID}", "title": "t", "body": "b"})
    assert result.ok, result.error
    assert sent[0].fid == FID and sent[0].token is None


def test_fid_falls_back_to_token_field_on_older_admin_sdks(monkeypatch):
    provider, sent = _provider(monkeypatch, _Message)  # no `fid` keyword
    result = provider.send({"push_id": "p1", "push_token": f"fid:{FID}"})
    assert result.ok, result.error
    assert sent[0].token == FID


def test_legacy_registration_tokens_still_send(monkeypatch):
    provider, sent = _provider(monkeypatch, _MessageWithFid)
    assert provider.send({"push_id": "p1", "push_token": LEGACY_TOKEN}).ok
    assert sent[0].token == LEGACY_TOKEN and sent[0].fid is None


def test_push_token_route_stores_typed_fid(tmp_path, monkeypatch):
    from omnidesk_agent.appsync.store import AppSyncStore

    store = AppSyncStore(tmp_path / "appsync.json")
    store.register_device(actor="alice", device_id="mobile-1", device_type="mobile", name="Phone", platform="ios")
    stored = normalize_push_target(FID, "fid")
    device = store.register_push_token(actor="alice", device_id="mobile-1", push_token=stored, platform="flutter")
    assert device["push_token"] == f"fid:{FID}"
    assert push.split_push_target(device["push_token"]) == ("fid", FID)


def _service_account():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    account = {"client_email": "push@omni-project.iam.gserviceaccount.com", "private_key": pem, "project_id": "omni-project", "token_uri": "https://oauth2.googleapis.com/token"}
    return account, key.public_key()


def _http_v1(account, responses):
    import httpx

    requests = []

    def handler(request):
        requests.append(request)
        return responses(request)

    client = push.FcmHttpV1Client(account, http=httpx.Client(transport=httpx.MockTransport(handler)))
    return client, requests


def test_http_v1_client_sends_fid_with_service_account_oauth():
    import base64 as b64
    import json as js
    from urllib.parse import parse_qs

    import httpx
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    account, public_key = _service_account()

    def responses(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3600})
        return httpx.Response(200, json={"name": "projects/omni-project/messages/42"})

    client, requests = _http_v1(account, responses)
    provider = FirebasePushProvider(http_client=client)
    for push_id in ("p1", "p2"):
        result = provider.send({"push_id": push_id, "push_token": f"fid:{FID}", "title": "Approval", "body": "Needed"})
        assert result.ok, result.error
        assert result.provider_message_id == "projects/omni-project/messages/42"

    token_requests = [r for r in requests if r.url.host == "oauth2.googleapis.com"]
    send_requests = [r for r in requests if r.url.host == "fcm.googleapis.com"]
    assert len(token_requests) == 1, "access token is cached"
    assertion = parse_qs(token_requests[0].content.decode())["assertion"][0]
    header, claims, signature = assertion.split(".")
    pad = lambda part: part + "=" * (-len(part) % 4)  # noqa: E731
    public_key.verify(b64.urlsafe_b64decode(pad(signature)), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    claim_set = js.loads(b64.urlsafe_b64decode(pad(claims)))
    assert claim_set["aud"] == "https://oauth2.googleapis.com/token"
    assert claim_set["scope"] == "https://www.googleapis.com/auth/firebase.messaging"

    assert send_requests[0].url.path == "/v1/projects/omni-project/messages:send"
    assert send_requests[0].headers["authorization"] == "Bearer ya29.test"
    body = js.loads(send_requests[0].content)
    assert body["message"]["fid"] == FID and "token" not in body["message"]
    assert body["message"]["notification"] == {"title": "Approval", "body": "Needed"}


def test_http_v1_client_uses_token_field_for_legacy_tokens_and_hides_targets_in_errors():
    import json as js

    import httpx

    account, _ = _service_account()

    def responses(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3600})
        return httpx.Response(404, json={"error": {"status": "UNREGISTERED", "message": f"token {LEGACY_TOKEN} not found"}})

    client, requests = _http_v1(account, responses)
    result = FirebasePushProvider(http_client=client).send({"push_id": "p1", "push_token": LEGACY_TOKEN})
    assert not result.ok
    assert result.error == "FCM send failed: HTTP 404 UNREGISTERED"
    assert LEGACY_TOKEN not in result.error
    assert js.loads(requests[-1].content)["message"]["token"] == LEGACY_TOKEN


def test_service_account_must_use_google_token_endpoint(monkeypatch):
    import json as js

    account, _ = _service_account()
    monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT_JSON", js.dumps({**account, "token_uri": "https://attacker.example/token"}))
    with pytest.raises(RuntimeError, match="token_uri"):
        push.load_service_account()
    monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT_JSON", js.dumps(account))
    assert push.load_service_account()["project_id"] == "omni-project"
