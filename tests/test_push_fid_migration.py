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
