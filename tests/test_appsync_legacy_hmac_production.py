"""Production refuses ``legacy-hmac:`` device keys.

A ``legacy-hmac:<secret>`` device "public key" is a symmetric HMAC secret that the
gateway stores as is. Test and development fixtures keep using it; in production
mode (``is_production_mode``) device registration, pairing, challenge
verification and device-signed requests refuse it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from typing import Callable

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from omnidesk_agent.appsync import factory
from omnidesk_agent.appsync.store import AppSyncStore, _approval_decision_message, _device_request_message, _sha256_hex
from omnidesk_agent.config import AppConfig
from omnidesk_agent.server import create_app

Signer = Callable[[bytes], str]

OWNER_AUTH = {"authorization": "Bearer owner-token", "content-type": "application/json"}
OPERATOR_AUTH = {"authorization": "Bearer operator-token", "content-type": "application/json"}
PAIRING_CODE = "pairing-code-123"


def _hmac_signer(secret: str) -> Signer:
    return lambda message: hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _ed25519() -> tuple[str, Signer]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo).decode("utf-8")
    return public, lambda message: private.sign(message).hex()


def _pair(store: AppSyncStore, *, actor: str, device_id: str, device_type: str, public_key: str) -> str:
    store.register_device(actor=actor, device_id=device_id, device_type=device_type, name=device_id, platform="test", public_key=public_key)
    enrollment = store.start_device_enrollment(actor="olivia", device_type=device_type, pairing_code=PAIRING_CODE)
    store.complete_device_enrollment(actor=actor, enrollment_id=enrollment["enrollment_id"], pairing_code=PAIRING_CODE, device_id=device_id, public_key=public_key)
    return enrollment["enrollment_id"]


def _enroll(store: AppSyncStore, *, actor: str, device_id: str, device_type: str, public_key: str, sign: Signer) -> None:
    enrollment_id = _pair(store, actor=actor, device_id=device_id, device_type=device_type, public_key=public_key)
    challenge = store.issue_device_challenge(actor=actor, enrollment_id=enrollment_id, device_id=device_id)
    store.verify_device_challenge(actor=actor, enrollment_id=enrollment_id, challenge_id=challenge["challenge_id"], device_id=device_id, signature=sign(challenge["signing_message"].encode("utf-8")))


def _signed_headers(*, device_id: str, sign: Signer, path: str, body: str) -> dict[str, str]:
    timestamp, nonce = str(int(time.time() * 1000)), uuid.uuid4().hex
    message = _device_request_message(method="POST", path=path, body_sha256=_sha256_hex(body.encode("utf-8")), timestamp=timestamp, nonce=nonce)
    return {"x-omnidesk-device-id": device_id, "x-omnidesk-timestamp": timestamp, "x-omnidesk-nonce": nonce, "x-omnidesk-device-signature": sign(message)}


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "workspace"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.learning.growth_plan_file = tmp_path / "growth.json"
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.token_file = tmp_path / "google" / "token.json"
    cfg.gateway.admin_allowed_ips = ["testclient", "127.0.0.1", "::1"]
    cfg.plugins.enabled = False
    cfg.channels.chrome.enabled = False
    return cfg


@pytest.fixture()
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A gateway built outside production; tests switch production on per request."""
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_OWNER_ACTOR", "olivia")
    monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", "oscar")
    app = create_app(_cfg(tmp_path))
    store = app.state.runtime.app_sync
    store.ensure_user("olivia")
    store.ensure_user("oscar")
    with TestClient(app) as client:
        yield client, store


def _register_payload(device_id: str, public_key: str) -> str:
    return json.dumps({"device_id": device_id, "device_type": "mobile", "name": "Phone", "platform": "flutter", "public_key": public_key})


# --- Routes in production mode ----------------------------------------------


def test_production_register_rejects_legacy_hmac_key(gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = gateway
    monkeypatch.setenv("OMNIDESK_ENV", "production")

    refused = client.post("/app/devices/register", headers={**OPERATOR_AUTH, "idempotency-key": "reg-1"}, content=_register_payload("dev_7c1e9a4b2d", "legacy-hmac:phone-secret"))
    assert refused.status_code == 422, refused.text
    assert "legacy-hmac" in refused.text and "phone-secret" not in refused.text
    assert "dev_7c1e9a4b2d" not in store.devices

    public_key, _ = _ed25519()
    accepted = client.post("/app/devices/register", headers={**OPERATOR_AUTH, "idempotency-key": "reg-2"}, content=_register_payload("dev_7c1e9a4b2d", public_key))
    assert accepted.status_code == 200, accepted.text
    assert store.devices["dev_7c1e9a4b2d"].public_key == public_key.strip()


def test_production_enrollment_completion_rejects_legacy_hmac_key(gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = gateway
    enrollment_id = store.start_device_enrollment(actor="olivia", device_type="mobile", pairing_code=PAIRING_CODE)["enrollment_id"]
    monkeypatch.setenv("OMNIDESK_ENV", "production")
    path = f"/app/devices/enrollment/{enrollment_id}/complete"

    # The store strips the key before binding it, so surrounding whitespace must not hide the prefix.
    for legacy_key in ("legacy-hmac:phone-secret", "  legacy-hmac:phone-secret\n"):
        refused = client.post(path, headers=OPERATOR_AUTH, json={"pairing_code": PAIRING_CODE, "device_id": "dev_5e2b8f0a71", "public_key": legacy_key})
        assert refused.status_code == 422, refused.text
        assert "legacy-hmac" in refused.text and "phone-secret" not in refused.text
    enrollment = store.device_enrollments[enrollment_id]
    assert (enrollment.status, enrollment.device_id, enrollment.public_key) == ("pending", None, None)

    public_key, _ = _ed25519()
    accepted = client.post(path, headers=OPERATOR_AUTH, json={"pairing_code": PAIRING_CODE, "device_id": "dev_5e2b8f0a71", "public_key": public_key})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["enrollment"]["status"] == "paired"


def test_production_challenge_verification_rejects_legacy_hmac_pairing(gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = gateway
    # Paired before production mode was switched on, e.g. an in-flight enrollment at upgrade time.
    enrollment_id = _pair(store, actor="oscar", device_id="dev_oscar_desk", device_type="desktop", public_key="legacy-hmac:desk-secret")
    monkeypatch.setenv("OMNIDESK_ENV", "production")

    issued = client.post(f"/app/devices/enrollment/{enrollment_id}/challenge", headers=OPERATOR_AUTH, json={"device_id": "dev_oscar_desk"})
    assert issued.status_code == 200, issued.text
    challenge = issued.json()["challenge"]
    signature = _hmac_signer("desk-secret")(challenge["signing_message"].encode("utf-8"))
    refused = client.post(f"/app/devices/enrollment/{enrollment_id}/verify", headers=OPERATOR_AUTH, json={"challenge_id": challenge["challenge_id"], "device_id": "dev_oscar_desk", "signature": signature})
    assert refused.status_code == 403, refused.text
    assert "legacy-hmac" in refused.text and "device_token" not in refused.text
    device = store.devices["dev_oscar_desk"]
    assert (device.credential_status, device.trust_level, device.token_hash) == ("enrolled", "public_key_registered", None)
    assert store.device_enrollments[enrollment_id].status == "paired"


def test_production_signed_request_rejects_verified_legacy_hmac_device(gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = gateway
    # Verified while legacy keys were still accepted.
    _enroll(store, actor="oscar", device_id="dev_oscar_legacy", device_type="desktop", public_key="legacy-hmac:desk-secret", sign=_hmac_signer("desk-secret"))
    public_key, sign = _ed25519()
    _enroll(store, actor="oscar", device_id="dev_oscar_ed25519", device_type="desktop", public_key=public_key, sign=sign)
    token_hash = store.devices["dev_oscar_legacy"].token_hash
    monkeypatch.setenv("OMNIDESK_ENV", "production")

    path = "/app/devices/dev_oscar_legacy/rotate-token"
    refused = client.post(path, headers={**OPERATOR_AUTH, **_signed_headers(device_id="dev_oscar_legacy", sign=_hmac_signer("desk-secret"), path=path, body="")}, content="")
    assert refused.status_code == 401, refused.text
    assert "legacy_hmac_key_not_allowed" in refused.text
    assert store.devices["dev_oscar_legacy"].token_hash == token_hash

    path = "/app/devices/dev_oscar_ed25519/rotate-token"
    accepted = client.post(path, headers={**OPERATOR_AUTH, **_signed_headers(device_id="dev_oscar_ed25519", sign=sign, path=path, body="")}, content="")
    assert accepted.status_code == 200, accepted.text


def test_production_legacy_hmac_device_re_enrolls_with_ed25519(gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = gateway
    _enroll(store, actor="oscar", device_id="dev_oscar_legacy", device_type="desktop", public_key="legacy-hmac:desk-secret", sign=_hmac_signer("desk-secret"))
    monkeypatch.setenv("OMNIDESK_ENV", "production")
    public_key, sign = _ed25519()

    started = client.post("/app/devices/enrollment/start", headers=OWNER_AUTH, json={"device_type": "desktop", "pairing_code": PAIRING_CODE})
    assert started.status_code == 200, started.text
    enrollment_id = started.json()["enrollment"]["enrollment_id"]
    paired = client.post(f"/app/devices/enrollment/{enrollment_id}/complete", headers=OPERATOR_AUTH, json={"pairing_code": PAIRING_CODE, "device_id": "dev_oscar_legacy", "public_key": public_key})
    assert paired.status_code == 200, paired.text
    challenge = client.post(f"/app/devices/enrollment/{enrollment_id}/challenge", headers=OPERATOR_AUTH, json={"device_id": "dev_oscar_legacy"}).json()["challenge"]
    verified = client.post(f"/app/devices/enrollment/{enrollment_id}/verify", headers=OPERATOR_AUTH, json={"challenge_id": challenge["challenge_id"], "device_id": "dev_oscar_legacy", "signature": sign(challenge["signing_message"].encode("utf-8"))})
    assert verified.status_code == 200, verified.text
    assert not store.devices["dev_oscar_legacy"].public_key.startswith("legacy-hmac:")

    path = "/app/devices/dev_oscar_legacy/rotate-token"
    stale = client.post(path, headers={**OPERATOR_AUTH, **_signed_headers(device_id="dev_oscar_legacy", sign=_hmac_signer("desk-secret"), path=path, body="")}, content="")
    assert stale.status_code == 401, stale.text
    rotated = client.post(path, headers={**OPERATOR_AUTH, **_signed_headers(device_id="dev_oscar_legacy", sign=sign, path=path, body="")}, content="")
    assert rotated.status_code == 200, rotated.text


def test_legacy_hmac_devices_still_enroll_outside_production(gateway) -> None:
    client, store = gateway
    registered = client.post("/app/devices/register", headers={**OPERATOR_AUTH, "idempotency-key": "reg-1"}, content=_register_payload("dev_oscar_phone", "legacy-hmac:phone-secret"))
    assert registered.status_code == 200, registered.text
    enrollment_id = store.start_device_enrollment(actor="olivia", device_type="mobile", pairing_code=PAIRING_CODE)["enrollment_id"]
    paired = client.post(f"/app/devices/enrollment/{enrollment_id}/complete", headers=OPERATOR_AUTH, json={"pairing_code": PAIRING_CODE, "device_id": "dev_oscar_phone", "public_key": "legacy-hmac:phone-secret"})
    assert paired.status_code == 200, paired.text
    challenge = client.post(f"/app/devices/enrollment/{enrollment_id}/challenge", headers=OPERATOR_AUTH, json={"device_id": "dev_oscar_phone"}).json()["challenge"]
    signature = _hmac_signer("phone-secret")(challenge["signing_message"].encode("utf-8"))
    verified = client.post(f"/app/devices/enrollment/{enrollment_id}/verify", headers=OPERATOR_AUTH, json={"challenge_id": challenge["challenge_id"], "device_id": "dev_oscar_phone", "signature": signature})
    assert verified.status_code == 200, verified.text
    assert store.devices["dev_oscar_phone"].trust_level == "challenge_verified"


# --- Store policy ------------------------------------------------------------


def test_store_policy_refuses_legacy_hmac_keys(tmp_path: Path) -> None:
    store = AppSyncStore(tmp_path / "appsync.json")
    store.ensure_user("olivia")
    store.ensure_user("oscar")
    _enroll(store, actor="oscar", device_id="dev_legacy_phone", device_type="mobile", public_key="legacy-hmac:phone-secret", sign=_hmac_signer("phone-secret"))
    pending_id = _pair(store, actor="oscar", device_id="dev_legacy_desk", device_type="desktop", public_key="legacy-hmac:desk-secret")
    public_key, sign = _ed25519()
    _enroll(store, actor="oscar", device_id="dev_ed25519_phone", device_type="mobile", public_key=public_key, sign=sign)
    store.devices["dev_legacy_phone"].approval_public_key = "legacy-hmac:approval-secret"

    store.configure_device_key_policy(allow_legacy_hmac=False)

    with pytest.raises(PermissionError, match="legacy-hmac"):
        store.register_device(actor="oscar", device_id="dev_new_phone", device_type="mobile", name="p", platform="ios", public_key="legacy-hmac:new-secret")
    assert "dev_new_phone" not in store.devices
    fresh_id = store.start_device_enrollment(actor="olivia", device_type="mobile", pairing_code=PAIRING_CODE)["enrollment_id"]
    with pytest.raises(ValueError, match="legacy-hmac"):
        store.complete_device_enrollment(actor="oscar", enrollment_id=fresh_id, pairing_code=PAIRING_CODE, device_id="dev_new_phone", public_key="legacy-hmac:new-secret")
    assert store.device_enrollments[fresh_id].status == "pending"

    challenge = store.issue_device_challenge(actor="oscar", enrollment_id=pending_id, device_id="dev_legacy_desk")
    with pytest.raises(PermissionError, match="legacy-hmac"):
        store.verify_device_challenge(actor="oscar", enrollment_id=pending_id, challenge_id=challenge["challenge_id"], device_id="dev_legacy_desk", signature=_hmac_signer("desk-secret")(challenge["signing_message"].encode("utf-8")))
    assert store.devices["dev_legacy_desk"].credential_status != "verified"

    def signed(device_id: str, signer: Signer) -> tuple[bool, str]:
        timestamp, nonce = str(time.time()), uuid.uuid4().hex
        message = _device_request_message(method="POST", path="/x", body_sha256=_sha256_hex(b"{}"), timestamp=timestamp, nonce=nonce)
        return store.verify_device_request_signature(device_id=device_id, method="POST", path="/x", body=b"{}", timestamp=timestamp, nonce=nonce, signature=signer(message), expected_actor="oscar")

    assert signed("dev_legacy_phone", _hmac_signer("phone-secret")) == (False, "legacy_hmac_key_not_allowed")
    assert signed("dev_ed25519_phone", sign) == (True, "ok")

    timestamp, nonce = str(time.time()), uuid.uuid4().hex
    approval_signature = _hmac_signer("approval-secret")(_approval_decision_message(approval_id="appr_1", decision="approved", timestamp=timestamp, nonce=nonce))
    assert store.verify_approval_signature(device_id="dev_legacy_phone", approval_id="appr_1", decision="approved", timestamp=timestamp, nonce=nonce, signature=approval_signature) == (False, "legacy_hmac_key_not_allowed")


def test_factory_refuses_legacy_hmac_keys_in_production(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Production always uses PostgreSQL; a JSON store stands in for it here.
    monkeypatch.setattr(factory, "MigratedMultiInstancePostgresAppSyncStore", lambda **_: AppSyncStore(tmp_path / "standin.json"))
    monkeypatch.setenv("OMNIDESK_APPSYNC_POSTGRES_DSN", "postgresql://stand-in")
    monkeypatch.setenv("OMNIDESK_ENV", "production")
    cfg = _cfg(tmp_path)
    cfg.app_sync.backend = "postgres"
    store = factory.create_appsync_store(cfg)
    with pytest.raises(PermissionError, match="legacy-hmac"):
        store.register_device(actor="oscar", device_id="dev_phone_9a8b", device_type="mobile", name="p", platform="ios", public_key="legacy-hmac:secret")

    monkeypatch.delenv("OMNIDESK_ENV")
    development = factory.create_appsync_store(_cfg(tmp_path / "dev"))
    assert development.register_device(actor="oscar", device_id="dev_phone_9a8b", device_type="mobile", name="p", platform="ios", public_key="legacy-hmac:secret")["credential_status"] == "enrolled"
