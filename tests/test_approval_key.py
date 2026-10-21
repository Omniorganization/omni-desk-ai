from __future__ import annotations

import base64
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import HTTPException

from omnidesk_agent.appsync.routes import _require_approval_signature
from omnidesk_agent.appsync.store import AppSyncStore


def _key() -> tuple[ed25519.Ed25519PrivateKey, str]:
    key = ed25519.Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, "base64:" + base64.b64encode(raw).decode()


def _verified_mobile(tmp_path: Path, device_type: str = "mobile") -> AppSyncStore:
    store = AppSyncStore(tmp_path / "appsync.json")
    store.register_device(actor="owner", device_id="mob_1", device_type=device_type, name="Phone", platform="ios", public_key="base64:" + "A" * 43 + "=")
    device = store.devices["mob_1"]
    device.credential_status = "verified"
    device.trust_level = "challenge_verified"
    return store


def _sign(key: ed25519.Ed25519PrivateKey, approval_id: str, decision: str, ts: str, nonce: str) -> str:
    message = f"omnidesk-approval-decision:v1:{approval_id}:{decision}:{ts}:{nonce}".encode()
    return "base64:" + base64.b64encode(key.sign(message)).decode()


def test_approval_key_is_write_once_and_needs_a_verified_mobile_device(tmp_path):
    store = _verified_mobile(tmp_path)
    _, public = _key()
    _, other = _key()
    with pytest.raises(ValueError):
        store.register_approval_key(actor="owner", device_id="mob_1", public_key="not-a-key")
    with pytest.raises(PermissionError, match="different actor"):
        store.register_approval_key(actor="mallory", device_id="mob_1", public_key=public)
    store.register_approval_key(actor="owner", device_id="mob_1", public_key=public)
    store.register_approval_key(actor="owner", device_id="mob_1", public_key=public)  # idempotent
    with pytest.raises(PermissionError, match="already registered"):
        store.register_approval_key(actor="owner", device_id="mob_1", public_key=other)

    unverified = AppSyncStore(tmp_path / "other.json")
    unverified.register_device(actor="owner", device_id="mob_2", device_type="mobile", name="Phone", platform="ios")
    with pytest.raises(PermissionError, match="challenge-verified"):
        unverified.register_approval_key(actor="owner", device_id="mob_2", public_key=public)

    desktop = _verified_mobile(tmp_path / "d", device_type="desktop") if (tmp_path / "d").mkdir() is None else None
    with pytest.raises(PermissionError, match="mobile"):
        desktop.register_approval_key(actor="owner", device_id="mob_1", public_key=public)


def test_approval_signature_binds_approval_decision_and_nonce(tmp_path):
    store = _verified_mobile(tmp_path)
    key, public = _key()
    store.register_approval_key(actor="owner", device_id="mob_1", public_key=public)
    ts, nonce = str(int(time.time() * 1000)), "a" * 32
    sig = _sign(key, "appr_1", "approved", ts, nonce)

    def check(approval_id="appr_1", decision="approved", t=ts, n=nonce, s=sig):
        return store.verify_approval_signature(device_id="mob_1", approval_id=approval_id, decision=decision, timestamp=t, nonce=n, signature=s)

    assert check(decision="rejected") == (False, "approval_signature_mismatch")
    assert check(approval_id="appr_2") == (False, "approval_signature_mismatch")
    assert check() == (True, "ok")
    assert check() == (False, "nonce_replay")
    old = str(int(time.time() - 3600))
    assert check(t=old, n="b" * 32, s=_sign(key, "appr_1", "approved", old, "b" * 32)) == (False, "timestamp_out_of_window")


def _request(headers: dict[str, str]):
    return SimpleNamespace(headers=headers)


def test_decision_route_requires_approval_signature_once_key_is_registered(tmp_path):
    store = _verified_mobile(tmp_path)
    cfg = SimpleNamespace(app_sync=SimpleNamespace(require_approval_key_for_decisions=False))
    # No approval key yet: legacy decisions still pass.
    _require_approval_signature(cfg=cfg, store=store, request=_request({}), approval_id="appr_1", verdict="approved", device_id="mob_1")

    key, public = _key()
    store.register_approval_key(actor="owner", device_id="mob_1", public_key=public)
    with pytest.raises(HTTPException) as exc:
        _require_approval_signature(cfg=cfg, store=store, request=_request({}), approval_id="appr_1", verdict="approved", device_id="mob_1")
    assert exc.value.status_code == 401 and "missing_approval_signature" in exc.value.detail

    ts, nonce = str(int(time.time())), "c" * 32
    headers = {
        "x-omnidesk-approval-timestamp": ts,
        "x-omnidesk-approval-nonce": nonce,
        "x-omnidesk-approval-signature": _sign(key, "appr_1", "approved", ts, nonce),
    }
    _require_approval_signature(cfg=cfg, store=store, request=_request(headers), approval_id="appr_1", verdict="approved", device_id="mob_1")


def test_require_flag_rejects_decisions_without_any_approval_key(tmp_path):
    store = _verified_mobile(tmp_path)
    cfg = SimpleNamespace(app_sync=SimpleNamespace(require_approval_key_for_decisions=True))
    with pytest.raises(HTTPException) as exc:
        _require_approval_signature(cfg=cfg, store=store, request=_request({}), approval_id="appr_1", verdict="approved", device_id="")
    assert "missing_device_id" in exc.value.detail
    with pytest.raises(HTTPException) as exc:
        _require_approval_signature(cfg=cfg, store=store, request=_request({}), approval_id="appr_1", verdict="approved", device_id="mob_1")
    assert "approval_key_not_registered" in exc.value.detail
