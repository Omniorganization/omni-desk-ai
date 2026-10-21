"""Regression tests for the gateway device-trust and task re-queue chain.

Covers the 2026-10-21 review findings H1 (enrollment bypass), M1 (task re-queue
and lease-less status writes), M4 (device signatures not bound to the actor) and
M5 (unsigned changes to verified devices). The two ``*_poc`` tests replay the
review's proof-of-concept scripts step by step and assert each attack step fails.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from omnidesk_agent.appsync.store import AppSyncStore, _device_request_message, _sha256_hex
from omnidesk_agent.config import AppConfig
from omnidesk_agent.server import create_app

OWNER = "token:OMNIDESK_OWNER_TOKEN"
OPERATOR = "token:OMNIDESK_OPERATOR_TOKEN"


def _hmac_sign(secret: str, message: str | bytes) -> str:
    data = message.encode("utf-8") if isinstance(message, str) else message
    return hmac.new(secret.encode("utf-8"), data, hashlib.sha256).hexdigest()


def _signed_request(store: AppSyncStore, *, device_id: str, secret: str, actor: str, path: str, body: bytes, types: set[str] | None = None) -> tuple[bool, str]:
    timestamp, nonce = str(time.time()), uuid.uuid4().hex
    message = _device_request_message(method="POST", path=path, body_sha256=_sha256_hex(body), timestamp=timestamp, nonce=nonce)
    return store.verify_device_request_signature(
        device_id=device_id, method="POST", path=path, body=body, timestamp=timestamp, nonce=nonce,
        signature=_hmac_sign(secret, message), expected_actor=actor, required_device_types=types,
    )


def _enroll(store: AppSyncStore, *, owner: str, actor: str, device_id: str, device_type: str, secret: str) -> dict:
    """Legitimate flow: owner starts, device holder pairs with the code, then signs the challenge."""
    public_key = f"legacy-hmac:{secret}"
    store.register_device(actor=actor, device_id=device_id, device_type=device_type, name=device_id, platform="test", public_key=public_key)
    enrollment = store.start_device_enrollment(actor=owner, device_type=device_type, pairing_code="pairing-code-123")
    paired = store.complete_device_enrollment(actor=actor, enrollment_id=enrollment["enrollment_id"], pairing_code="pairing-code-123", device_id=device_id, public_key=public_key)
    assert paired["status"] == "paired"
    challenge = store.issue_device_challenge(actor=actor, enrollment_id=enrollment["enrollment_id"], device_id=device_id)
    store.verify_device_challenge(actor=actor, enrollment_id=enrollment["enrollment_id"], challenge_id=challenge["challenge_id"], device_id=device_id, signature=_hmac_sign(secret, challenge["signing_message"]))
    return enrollment


@pytest.fixture()
def store(tmp_path: Path) -> AppSyncStore:
    store = AppSyncStore(tmp_path / "appsync.json")
    store.ensure_user(OWNER)
    store.ensure_user(OPERATOR)
    return store


# --- H1: device enrollment -------------------------------------------------


def test_enroll_bypass_poc_is_rejected(store: AppSyncStore) -> None:
    enrollment = store.start_device_enrollment(actor=OWNER, device_type="mobile", pairing_code="owner-secret-pairing-code")
    enrollment_id = enrollment["enrollment_id"]
    # [1] The enrollment id is still visible to the operator through /app/sync.
    leaked = [e for e in store.sync_since(0, actor=OPERATOR)["events"] if e["event_type"] == "device.enrollment_started"]
    assert leaked[0]["payload"]["enrollment_id"] == enrollment_id
    # [2] The attacker registers its own desktop with its own key.
    secret = "attacker-key"
    store.register_device(actor=OPERATOR, device_id="dev_attacker_9f3a1c", device_type="desktop", name="x", platform="linux", public_key="legacy-hmac:" + secret)
    # [3] Without the pairing code the enrollment never reaches a challenge.
    with pytest.raises(ValueError, match="not awaiting device verification"):
        store.issue_device_challenge(actor=OPERATOR, enrollment_id=enrollment_id, device_id="dev_attacker_9f3a1c")
    assert not store.device_challenges
    # [4] The device stays unverified.
    device = store.devices["dev_attacker_9f3a1c"]
    assert (device.credential_status, device.trust_level) == ("enrolled", "public_key_registered")
    # [5] Desktop-only signed routes reject it.
    assert _signed_request(store, device_id="dev_attacker_9f3a1c", secret=secret, actor=OPERATOR, path="/app/runtime/desktop/claim", body=b"{}", types={"desktop"}) == (False, "device_not_challenge_verified")
    assert store.device_enrollments[enrollment_id].status == "pending"


def test_completed_enrollment_cannot_verify_another_device(store: AppSyncStore) -> None:
    enrollment = _enroll(store, owner=OWNER, actor=OPERATOR, device_id="dev_phone_1", device_type="mobile", secret="k1")
    record = store.device_enrollments[enrollment["enrollment_id"]]
    assert record.status == "completed" and record.device_id == "dev_phone_1"
    # [6] of the PoC: replaying the spent enrollment for a second device fails.
    store.register_device(actor=OPERATOR, device_id="dev_phone_2", device_type="mobile", name="y", platform="ios", public_key="legacy-hmac:k1")
    for device_id in ("dev_phone_2", "dev_phone_1"):
        with pytest.raises(ValueError, match="not awaiting device verification"):
            store.issue_device_challenge(actor=OPERATOR, enrollment_id=enrollment["enrollment_id"], device_id=device_id)
    with pytest.raises(ValueError, match="not pending"):
        store.complete_device_enrollment(actor=OPERATOR, enrollment_id=enrollment["enrollment_id"], pairing_code="pairing-code-123", device_id="dev_phone_2", public_key="legacy-hmac:k1")
    assert store.devices["dev_phone_2"].credential_status == "enrolled"


def test_enrollment_binds_device_type_actor_and_id(store: AppSyncStore) -> None:
    store.register_device(actor=OPERATOR, device_id="dev_desk", device_type="desktop", name="d", platform="mac", public_key="legacy-hmac:k")
    store.register_device(actor=OWNER, device_id="dev_owner_phone", device_type="mobile", name="p", platform="ios", public_key="legacy-hmac:k")
    mobile = store.start_device_enrollment(actor=OWNER, device_type="mobile", pairing_code="pairing-code-123")
    with pytest.raises(PermissionError, match="device type"):
        store.complete_device_enrollment(actor=OPERATOR, enrollment_id=mobile["enrollment_id"], pairing_code="pairing-code-123", device_id="dev_desk", public_key="legacy-hmac:k")
    with pytest.raises(PermissionError, match="different actor"):
        store.complete_device_enrollment(actor=OPERATOR, enrollment_id=mobile["enrollment_id"], pairing_code="pairing-code-123", device_id="dev_owner_phone", public_key="legacy-hmac:k")
    with pytest.raises(ValueError, match="pairing code"):
        store.complete_device_enrollment(actor=OPERATOR, enrollment_id=mobile["enrollment_id"], pairing_code="wrong-code-000", device_id="dev_desk", public_key="legacy-hmac:k")
    assert store.device_enrollments[mobile["enrollment_id"]].status == "pending"

    store.register_device(actor=OPERATOR, device_id="dev_phone", device_type="mobile", name="p", platform="ios", public_key="legacy-hmac:k")
    store.register_device(actor=OPERATOR, device_id="dev_phone_other", device_type="mobile", name="p", platform="ios", public_key="legacy-hmac:k")
    store.complete_device_enrollment(actor=OPERATOR, enrollment_id=mobile["enrollment_id"], pairing_code="pairing-code-123", device_id="dev_phone", public_key="legacy-hmac:k")
    with pytest.raises(PermissionError, match="does not match the enrollment"):
        store.issue_device_challenge(actor=OPERATOR, enrollment_id=mobile["enrollment_id"], device_id="dev_phone_other")
    with pytest.raises(PermissionError, match="different actor"):
        store.issue_device_challenge(actor=OWNER, enrollment_id=mobile["enrollment_id"], device_id="dev_phone")
    # Relabelling the paired device before verification is caught at verify time.
    challenge = store.issue_device_challenge(actor=OPERATOR, enrollment_id=mobile["enrollment_id"], device_id="dev_phone")
    store.devices["dev_phone"].device_type = "desktop"
    with pytest.raises(PermissionError, match="device type"):
        store.verify_device_challenge(actor=OPERATOR, enrollment_id=mobile["enrollment_id"], challenge_id=challenge["challenge_id"], device_id="dev_phone", signature=_hmac_sign("k", challenge["signing_message"]))


def test_challenge_is_verified_against_the_enrollment_key(store: AppSyncStore) -> None:
    store.register_device(actor=OPERATOR, device_id="dev_phone", device_type="mobile", name="p", platform="ios", public_key="legacy-hmac:device-registered-key")
    enrollment = store.start_device_enrollment(actor=OWNER, device_type="mobile", pairing_code="pairing-code-123")
    store.complete_device_enrollment(actor=OPERATOR, enrollment_id=enrollment["enrollment_id"], pairing_code="pairing-code-123", device_id="dev_phone", public_key="legacy-hmac:paired-key")
    challenge = store.issue_device_challenge(actor=OPERATOR, enrollment_id=enrollment["enrollment_id"], device_id="dev_phone")
    with pytest.raises(ValueError, match="signature mismatch"):
        store.verify_device_challenge(actor=OPERATOR, enrollment_id=enrollment["enrollment_id"], challenge_id=challenge["challenge_id"], device_id="dev_phone", signature=_hmac_sign("device-registered-key", challenge["signing_message"]))
    second = store.issue_device_challenge(actor=OPERATOR, enrollment_id=enrollment["enrollment_id"], device_id="dev_phone")
    store.verify_device_challenge(actor=OPERATOR, enrollment_id=enrollment["enrollment_id"], challenge_id=second["challenge_id"], device_id="dev_phone", signature=_hmac_sign("paired-key", second["signing_message"]))
    device = store.devices["dev_phone"]
    assert (device.credential_status, device.public_key) == ("verified", "legacy-hmac:paired-key")
    # The other outstanding challenge is spent together with the enrollment.
    assert store.device_challenges[challenge["challenge_id"]].status == "expired"


def test_expired_pairing_cannot_be_verified(store: AppSyncStore) -> None:
    store.register_device(actor=OPERATOR, device_id="dev_phone", device_type="mobile", name="p", platform="ios", public_key="legacy-hmac:k")
    enrollment = store.start_device_enrollment(actor=OWNER, device_type="mobile", pairing_code="pairing-code-123")
    store.complete_device_enrollment(actor=OPERATOR, enrollment_id=enrollment["enrollment_id"], pairing_code="pairing-code-123", device_id="dev_phone", public_key="legacy-hmac:k")
    challenge = store.issue_device_challenge(actor=OPERATOR, enrollment_id=enrollment["enrollment_id"], device_id="dev_phone")
    store.device_enrollments[enrollment["enrollment_id"]].expires_at = time.time() - 1
    with pytest.raises(ValueError, match="enrollment expired"):
        store.verify_device_challenge(actor=OPERATOR, enrollment_id=enrollment["enrollment_id"], challenge_id=challenge["challenge_id"], device_id="dev_phone", signature=_hmac_sign("k", challenge["signing_message"]))
    assert store.device_enrollments[enrollment["enrollment_id"]].status == "expired"
    assert store.devices["dev_phone"].credential_status == "enrolled"


def test_ed25519_enrollment_still_verifies(store: AppSyncStore) -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo).decode("utf-8")
    store.register_device(actor=OPERATOR, device_id="dev_desk", device_type="desktop", name="d", platform="mac", public_key=public)
    enrollment = store.start_device_enrollment(actor=OWNER, device_type="desktop", pairing_code="pairing-code-123")
    store.complete_device_enrollment(actor=OPERATOR, enrollment_id=enrollment["enrollment_id"], pairing_code="pairing-code-123", device_id="dev_desk", public_key=public)
    challenge = store.issue_device_challenge(actor=OPERATOR, enrollment_id=enrollment["enrollment_id"], device_id="dev_desk")
    signature = private.sign(challenge["signing_message"].encode("utf-8")).hex()
    credential = store.verify_device_challenge(actor=OPERATOR, enrollment_id=enrollment["enrollment_id"], challenge_id=challenge["challenge_id"], device_id="dev_desk", signature=signature)
    assert credential["trust_level"] == "challenge_verified"
    assert store.device_enrollments[enrollment["enrollment_id"]].status == "completed"


# --- M1: task status, leases and approval consumption ------------------------


def _approved_desktop_task(store: AppSyncStore) -> tuple[str, str]:
    conversation = store.create_conversation(actor=OWNER, title="ops")
    created = store.add_message_and_task(actor=OWNER, conversation_id=conversation["conversation_id"], content="Wire $10k to vendor (desktop action)", requires_desktop_runtime=True, risk="high")
    task_id, approval_id = created["task"]["task_id"], created["approval"]["approval_id"]
    store.decide_approval(approval_id=approval_id, actor=OWNER, decision="approved")
    return task_id, approval_id


def test_task_requeue_poc_is_rejected(store: AppSyncStore) -> None:
    store.register_device(actor=OWNER, device_id="dev_real_desktop_1a2b", device_type="desktop", name="real", platform="mac", public_key="legacy-hmac:k1")
    store.register_device(actor=OPERATOR, device_id="dev_rogue_desktop_3c4d", device_type="desktop", name="rogue", platform="linux", public_key="legacy-hmac:k2")
    task_id, approval_id = _approved_desktop_task(store)
    # [1] The real desktop claims the task, consuming its approval.
    claimed = store.claim_next_task(actor=OWNER, device_id="dev_real_desktop_1a2b")
    assert claimed and claimed["task_id"] == task_id and claimed["attempt_count"] == 1
    assert store.approvals[approval_id].status == "consumed"
    # While running, the operator's rogue device cannot write status or results.
    with pytest.raises(PermissionError, match="lease"):
        store.update_task_status(task_id=task_id, actor=OPERATOR, status="completed", result_summary="FAKE RESULT", device_id="dev_rogue_desktop_3c4d")
    with pytest.raises(PermissionError, match="lease"):
        store.update_task_status(task_id=task_id, actor=OPERATOR, status="queued")
    # [2] The lease holder finishes the task.
    store.update_task_status(task_id=task_id, actor=OWNER, status="completed", result_summary="done", device_id="dev_real_desktop_1a2b")
    # [3] Re-queueing the completed task is refused and nothing can re-claim it.
    with pytest.raises(ValueError, match="already completed"):
        store.update_task_status(task_id=task_id, actor=OPERATOR, status="queued")
    assert store.claim_next_task(actor=OWNER, device_id="dev_real_desktop_1a2b") is None
    # [4] The result cannot be overwritten afterwards either.
    with pytest.raises(ValueError, match="already completed"):
        store.update_task_status(task_id=task_id, actor=OPERATOR, status="completed", result_summary="FAKE RESULT", device_id="dev_real_desktop_1a2b")
    task = store.tasks[task_id]
    assert (task.status, task.result_summary, task.attempt_count) == ("completed", "done", 1)


def test_requeued_task_cannot_reuse_a_consumed_approval(store: AppSyncStore) -> None:
    store.register_device(actor=OWNER, device_id="dev_desk", device_type="desktop", name="d", platform="mac")
    task_id, approval_id = _approved_desktop_task(store)
    assert store.claim_next_task(actor=OWNER, device_id="dev_desk")
    # Even if some future path put the task back in the queue, the spent approval
    # no longer authorizes a run.
    store.tasks[task_id].status = "queued"
    assert store.claim_next_task(actor=OWNER, device_id="dev_desk") is None
    assert store.approvals[approval_id].status == "consumed"
    with pytest.raises(ValueError, match="already decided"):
        store.decide_approval(approval_id=approval_id, actor=OWNER, decision="approved")


def test_lease_holder_rules(store: AppSyncStore) -> None:
    store.register_device(actor=OWNER, device_id="dev_desk", device_type="desktop", name="d", platform="mac")
    task_id, _ = _approved_desktop_task(store)
    store.claim_next_task(actor=OWNER, device_id="dev_desk")
    with pytest.raises(PermissionError, match="re-queued"):
        store.update_task_status(task_id=task_id, actor=OWNER, status="queued", device_id="dev_desk")
    with pytest.raises(ValueError, match="approval flow"):
        store.update_task_status(task_id=task_id, actor=OWNER, status="blocked", device_id="dev_desk")
    with pytest.raises(PermissionError, match="must match the reporting device"):
        store.update_task_status(task_id=task_id, actor=OWNER, status="completed", device_id="dev_desk", assigned_runtime_device_id="dev_other")
    # The holder's device has to belong to the reporting actor.
    with pytest.raises(PermissionError, match="different actor"):
        store.update_task_status(task_id=task_id, actor=OPERATOR, status="failed", device_id="dev_desk")
    running = store.update_task_status(task_id=task_id, actor=OWNER, status="running", result_summary="halfway", assigned_runtime_device_id="dev_desk")
    assert running["claimed_by_device_id"] == "dev_desk"
    assert store.update_task_status(task_id=task_id, actor=OWNER, status="failed", device_id="dev_desk")["lease_expires_at"] is None


def test_expired_run_is_not_reclaimed_under_the_spent_approval(store: AppSyncStore) -> None:
    store.register_device(actor=OWNER, device_id="dev_a", device_type="desktop", name="a", platform="mac")
    store.register_device(actor=OPERATOR, device_id="dev_b", device_type="desktop", name="b", platform="mac")
    task_id, _ = _approved_desktop_task(store)
    store.claim_next_task(actor=OWNER, device_id="dev_a")
    store.tasks[task_id].lease_expires_at = time.time() - 1
    # Another desktop must not re-run an approved action once its lease lapses.
    assert store.claim_next_task(actor=OPERATOR, device_id="dev_b") is None
    with pytest.raises(PermissionError, match="lease"):
        store.update_task_status(task_id=task_id, actor=OPERATOR, status="completed", result_summary="FAKE", device_id="dev_b")
    with pytest.raises(PermissionError, match="lease"):
        store.update_task_status(task_id=task_id, actor=OPERATOR, status="cancelled", device_id="dev_b")
    # The holder can still report a late result.
    assert store.update_task_status(task_id=task_id, actor=OWNER, status="completed", result_summary="done", device_id="dev_a")["attempt_count"] == 1


def test_creator_can_cancel_an_abandoned_run(store: AppSyncStore) -> None:
    store.register_device(actor=OWNER, device_id="dev_a", device_type="desktop", name="a", platform="mac")
    task_id, _ = _approved_desktop_task(store)
    store.claim_next_task(actor=OWNER, device_id="dev_a")
    with pytest.raises(PermissionError, match="lease"):
        store.update_task_status(task_id=task_id, actor=OWNER, status="cancelled")
    store.tasks[task_id].lease_expires_at = time.time() - 1
    assert store.update_task_status(task_id=task_id, actor=OWNER, status="cancelled")["status"] == "cancelled"


def test_task_status_requires_the_actor_organization(store: AppSyncStore) -> None:
    store.register_device(actor=OWNER, device_id="dev_desk", device_type="desktop", name="d", platform="mac")
    store.register_device(actor="mallory", device_id="dev_mallory", device_type="desktop", name="m", platform="linux", organization_id="org_other")
    task_id, _ = _approved_desktop_task(store)
    store.claim_next_task(actor=OWNER, device_id="dev_desk")
    with pytest.raises(PermissionError, match="organization"):
        store.update_task_status(task_id=task_id, actor="mallory", status="completed", device_id="dev_desk", assigned_runtime_device_id="dev_desk")


def test_unclaimed_task_can_only_be_cancelled_by_its_creator(store: AppSyncStore) -> None:
    conversation = store.create_conversation(actor=OWNER, title="ops")
    created = store.add_message_and_task(actor=OWNER, conversation_id=conversation["conversation_id"], content="desktop action", requires_desktop_runtime=True)
    task_id = created["task"]["task_id"]
    for status in ("queued", "running", "completed"):
        with pytest.raises(PermissionError, match="unclaimed"):
            store.update_task_status(task_id=task_id, actor=OWNER, status=status)
    with pytest.raises(PermissionError, match="unclaimed"):
        store.update_task_status(task_id=task_id, actor=OPERATOR, status="cancelled")
    assert store.update_task_status(task_id=task_id, actor=OWNER, status="cancelled")["status"] == "cancelled"
    # Approving the still-pending approval afterwards does not revive the task.
    store.decide_approval(approval_id=created["approval"]["approval_id"], actor=OWNER, decision="approved")
    assert store.tasks[task_id].status == "cancelled"
    store.register_device(actor=OWNER, device_id="dev_desk", device_type="desktop", name="d", platform="mac")
    assert store.claim_next_task(actor=OWNER, device_id="dev_desk") is None


def test_task_status_idempotent_replay_returns_the_first_result(store: AppSyncStore) -> None:
    store.register_device(actor=OWNER, device_id="dev_desk", device_type="desktop", name="d", platform="mac")
    task_id, _ = _approved_desktop_task(store)
    store.claim_next_task(actor=OWNER, device_id="dev_desk")
    kwargs = dict(task_id=task_id, actor=OWNER, status="completed", result_summary="done", device_id="dev_desk", idempotency_key="k1", idempotency_payload={"status": "completed"})
    first = store.update_task_status(**kwargs)
    assert store.update_task_status(**kwargs) == first


# --- M4: device signatures are bound to the authenticated actor --------------


def test_device_signature_is_bound_to_the_actor(store: AppSyncStore) -> None:
    _enroll(store, owner=OWNER, actor=OPERATOR, device_id="dev_operator_phone", device_type="mobile", secret="op-key")
    path, body = "/app/approvals/appr_x/decide", b'{"decision":"approved"}'
    assert _signed_request(store, device_id="dev_operator_phone", secret="op-key", actor=OWNER, path=path, body=body, types={"mobile"}) == (False, "device_actor_mismatch")
    assert _signed_request(store, device_id="dev_operator_phone", secret="op-key", actor="", path=path, body=body, types={"mobile"}) == (False, "device_actor_mismatch")
    assert _signed_request(store, device_id="dev_operator_phone", secret="op-key", actor=OPERATOR, path=path, body=body, types={"mobile"}) == (True, "ok")


def test_device_signature_rejects_a_device_outside_the_actor_organization(store: AppSyncStore) -> None:
    _enroll(store, owner=OWNER, actor=OPERATOR, device_id="dev_phone", device_type="mobile", secret="k")
    store.devices["dev_phone"].organization_id = "org_other"
    assert _signed_request(store, device_id="dev_phone", secret="k", actor=OPERATOR, path="/x", body=b"{}") == (False, "device_organization_mismatch")


# --- M5: verified devices cannot be changed by an unsigned re-registration ----


def test_verified_device_protected_fields_need_signature_or_reenrollment(store: AppSyncStore) -> None:
    _enroll(store, owner=OWNER, actor=OWNER, device_id="dev_phone", device_type="mobile", secret="k")
    store.register_push_token(actor=OWNER, device_id="dev_phone", push_token="owner-push", platform="ios")
    base = dict(actor=OWNER, device_id="dev_phone", device_type="mobile", name="p", platform="test", public_key="legacy-hmac:k")

    assert store.register_device(**base)["credential_status"] == "verified"
    with pytest.raises(PermissionError, match="type"):
        store.register_device(**{**base, "device_type": "web_admin"})
    with pytest.raises(PermissionError, match="push target"):
        store.register_device(**base, push_token="attacker-push")
    with pytest.raises(PermissionError, match="capabilities"):
        store.register_device(**base, capabilities=["approval", "governance"])
    with pytest.raises(PermissionError, match="tokens"):
        store.register_device(**base, token_hash="attacker-chosen-hash")
    device = store.devices["dev_phone"]
    assert (device.device_type, device.push_token, device.capabilities) == ("mobile", "owner-push", [])

    # A request signed by the device itself may refresh push target and capabilities,
    # but the type stays bound to the enrollment.
    updated = store.register_device(**base, push_token="rotated-push", capabilities=["approval"], device_signature_verified=True)
    assert (updated["push_token"], updated["capabilities"], updated["credential_status"]) == ("rotated-push", ["approval"], "verified")
    with pytest.raises(PermissionError, match="type"):
        store.register_device(**{**base, "device_type": "desktop"}, device_signature_verified=True)


def test_push_token_route_store_requires_the_device_owner(store: AppSyncStore) -> None:
    _enroll(store, owner=OWNER, actor=OWNER, device_id="dev_phone", device_type="mobile", secret="k")
    with pytest.raises(PermissionError, match="different actor"):
        store.register_push_token(actor=OPERATOR, device_id="dev_phone", push_token="attacker-push")
    assert store.devices["dev_phone"].push_token is None


def test_unverified_device_registration_is_unchanged(store: AppSyncStore) -> None:
    store.register_device(actor=OPERATOR, device_id="dev_new", device_type="mobile", name="p", platform="ios")
    changed = store.register_device(actor=OPERATOR, device_id="dev_new", device_type="desktop", name="p", platform="ios", push_token="push", capabilities=["x"])
    assert (changed["device_type"], changed["push_token"], changed["capabilities"]) == ("desktop", "push", ["x"])


# --- Route-level checks ------------------------------------------------------


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


def _signed_headers(*, device_id: str, secret: str, path: str, body: str) -> dict[str, str]:
    timestamp, nonce = str(int(time.time() * 1000)), uuid.uuid4().hex
    message = _device_request_message(method="POST", path=path, body_sha256=_sha256_hex(body.encode("utf-8")), timestamp=timestamp, nonce=nonce)
    return {"x-omnidesk-device-id": device_id, "x-omnidesk-timestamp": timestamp, "x-omnidesk-nonce": nonce, "x-omnidesk-device-signature": _hmac_sign(secret, message)}


@pytest.fixture()
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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


OWNER_AUTH = {"authorization": "Bearer owner-token", "content-type": "application/json"}
OPERATOR_AUTH = {"authorization": "Bearer operator-token", "content-type": "application/json"}


def test_route_approval_decision_rejects_another_actors_device(gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = gateway
    _enroll(store, owner="olivia", actor="oscar", device_id="dev_oscar_phone", device_type="mobile", secret="oscar-key")
    _enroll(store, owner="olivia", actor="olivia", device_id="dev_olivia_phone", device_type="mobile", secret="olivia-key")
    conversation = store.create_conversation(actor="olivia", title="ops")
    created = store.add_message_and_task(actor="olivia", conversation_id=conversation["conversation_id"], content="desktop action", requires_desktop_runtime=True)
    path = f"/app/approvals/{created['approval']['approval_id']}/decide"
    monkeypatch.setenv("OMNIDESK_ENV", "production")

    body = json.dumps({"decision": "approved", "source_device_id": "dev_oscar_phone"})
    denied = client.post(path, headers={**OWNER_AUTH, "idempotency-key": "d1", **_signed_headers(device_id="dev_oscar_phone", secret="oscar-key", path=path, body=body)}, content=body)
    assert denied.status_code == 401 and "device_actor_mismatch" in denied.text
    assert store.approvals[created["approval"]["approval_id"]].status == "pending"

    body = json.dumps({"decision": "approved", "source_device_id": "dev_olivia_phone"})
    accepted = client.post(path, headers={**OWNER_AUTH, "idempotency-key": "d2", **_signed_headers(device_id="dev_olivia_phone", secret="olivia-key", path=path, body=body)}, content=body)
    assert accepted.status_code == 200, accepted.text


def test_route_task_status_requires_the_lease_holder(gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = gateway
    _enroll(store, owner="olivia", actor="oscar", device_id="dev_oscar_desk", device_type="desktop", secret="oscar-desk")
    _enroll(store, owner="olivia", actor="olivia", device_id="dev_olivia_desk", device_type="desktop", secret="olivia-desk")
    conversation = store.create_conversation(actor="olivia", title="ops")
    created = store.add_message_and_task(actor="olivia", conversation_id=conversation["conversation_id"], content="desktop action", requires_desktop_runtime=True)
    task_id = created["task"]["task_id"]
    store.decide_approval(approval_id=created["approval"]["approval_id"], actor="olivia", decision="approved")
    store.claim_next_task(actor="oscar", device_id="dev_oscar_desk")
    monkeypatch.setenv("OMNIDESK_ENV", "production")
    path = f"/app/tasks/{task_id}/status"

    # Another actor's verified desktop cannot report on a task it does not hold.
    body = json.dumps({"status": "completed", "result_summary": "FAKE"})
    rogue = client.post(path, headers={**OWNER_AUTH, "idempotency-key": "s1", **_signed_headers(device_id="dev_olivia_desk", secret="olivia-desk", path=path, body=body)}, content=body)
    assert rogue.status_code == 403 and "lease" in rogue.text
    # Nor can it sign as the lease holder's device.
    body = json.dumps({"status": "completed", "assigned_runtime_device_id": "dev_oscar_desk"})
    spoof = client.post(path, headers={**OWNER_AUTH, "idempotency-key": "s2", **_signed_headers(device_id="dev_oscar_desk", secret="oscar-desk", path=path, body=body)}, content=body)
    assert spoof.status_code == 401 and "device_actor_mismatch" in spoof.text

    body = json.dumps({"status": "completed", "result_summary": "done", "assigned_runtime_device_id": "dev_oscar_desk"})
    done = client.post(path, headers={**OPERATOR_AUTH, "idempotency-key": "s3", **_signed_headers(device_id="dev_oscar_desk", secret="oscar-desk", path=path, body=body)}, content=body)
    assert done.status_code == 200, done.text
    body = json.dumps({"status": "queued", "assigned_runtime_device_id": "dev_oscar_desk"})
    requeue = client.post(path, headers={**OPERATOR_AUTH, "idempotency-key": "s4", **_signed_headers(device_id="dev_oscar_desk", secret="oscar-desk", path=path, body=body)}, content=body)
    assert requeue.status_code == 409 and "already completed" in requeue.text
    assert store.tasks[task_id].result_summary == "done"


def test_route_reregistration_needs_device_signature_for_push_target(gateway) -> None:
    client, store = gateway
    _enroll(store, owner="olivia", actor="olivia", device_id="dev_olivia_phone", device_type="mobile", secret="olivia-key")
    path = "/app/devices/register"
    payload = {"device_id": "dev_olivia_phone", "device_type": "mobile", "name": "Phone", "platform": "flutter", "public_key": "legacy-hmac:olivia-key", "push_token": "attacker-fcm-registration-token", "push_token_type": "fcm_token"}

    body = json.dumps(payload)
    unsigned = client.post(path, headers={**OWNER_AUTH, "idempotency-key": "r1"}, content=body)
    assert unsigned.status_code == 403 and "push target" in unsigned.text
    # A signature from a different device does not count as the device's own.
    _enroll(store, owner="olivia", actor="olivia", device_id="dev_olivia_other", device_type="mobile", secret="other-key")
    wrong = client.post(path, headers={**OWNER_AUTH, "idempotency-key": "r2", **_signed_headers(device_id="dev_olivia_other", secret="other-key", path=path, body=body)}, content=body)
    assert wrong.status_code == 403 and "push target" in wrong.text
    assert store.devices["dev_olivia_phone"].push_token is None

    signed = client.post(path, headers={**OWNER_AUTH, "idempotency-key": "r3", **_signed_headers(device_id="dev_olivia_phone", secret="olivia-key", path=path, body=body)}, content=body)
    assert signed.status_code == 200, signed.text
    assert store.devices["dev_olivia_phone"].push_token == "attacker-fcm-registration-token"


# --- PostgreSQL store parity (runs where OMNIDESK_TEST_POSTGRES_DSN is set) ---


def _postgres_stores(prefix: str):
    dsn = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("OMNIDESK_TEST_POSTGRES_DSN is not configured")
    from omnidesk_agent.appsync.migrated_postgres_store import MigratedMultiInstancePostgresAppSyncStore
    from omnidesk_agent.appsync.postgres_migrations import apply_appsync_migrations

    namespace = f"{prefix}_{uuid.uuid4().hex}"
    apply_appsync_migrations(dsn, namespace=namespace)
    return (
        MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=namespace, pool_size=2),
        MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=namespace, pool_size=2),
    )


def test_postgres_enrollment_is_single_use_and_bound() -> None:
    first, second = _postgres_stores("device_trust")
    try:
        first.ensure_user(OWNER)
        first.ensure_user(OPERATOR)
        pending = first.start_device_enrollment(actor=OWNER, device_type="mobile", pairing_code="owner-secret-pairing-code")
        first.register_device(actor=OPERATOR, device_id="dev_attacker", device_type="desktop", name="x", platform="linux", public_key="legacy-hmac:attacker")
        with pytest.raises(ValueError, match="not awaiting device verification"):
            second.issue_device_challenge(actor=OPERATOR, enrollment_id=pending["enrollment_id"], device_id="dev_attacker")

        enrollment = _enroll(first, owner=OWNER, actor=OPERATOR, device_id="dev_phone", device_type="mobile", secret="k")
        second.sync_since(0, actor=OPERATOR)  # serialized call: reloads shared state
        assert second.device_enrollments[enrollment["enrollment_id"]].status == "completed"
        second.register_device(actor=OPERATOR, device_id="dev_phone_2", device_type="mobile", name="y", platform="ios", public_key="legacy-hmac:k")
        with pytest.raises(ValueError, match="not awaiting device verification"):
            second.issue_device_challenge(actor=OPERATOR, enrollment_id=enrollment["enrollment_id"], device_id="dev_phone_2")
        assert _signed_request(second, device_id="dev_phone", secret="k", actor=OWNER, path="/x", body=b"{}") == (False, "device_actor_mismatch")
        with pytest.raises(PermissionError, match="push target"):
            second.register_device(actor=OPERATOR, device_id="dev_phone", device_type="mobile", name="p", platform="test", push_token="attacker-push")
    finally:
        first.close()
        second.close()


def test_postgres_claim_consumes_approval_and_status_needs_the_lease() -> None:
    first, second = _postgres_stores("task_lease")
    try:
        first.ensure_user(OWNER)
        first.ensure_user(OPERATOR)
        first.register_device(actor=OWNER, device_id="dev_real", device_type="desktop", name="real", platform="mac")
        first.register_device(actor=OPERATOR, device_id="dev_rogue", device_type="desktop", name="rogue", platform="linux")
        first.ensure_user("mallory", organization_id="org_other")
        task_id, approval_id = _approved_desktop_task(first)

        claimed = second.claim_next_task(actor=OWNER, device_id="dev_real")
        assert claimed and claimed["task_id"] == task_id
        assert {a["approval_id"]: a["status"] for a in first.list_approvals(actor=OWNER)}[approval_id] == "consumed"
        assert second.claim_next_task(actor=OWNER, device_id="dev_real") is None
        with pytest.raises(PermissionError, match="lease"):
            first.update_task_status(task_id=task_id, actor=OPERATOR, status="completed", result_summary="FAKE", device_id="dev_rogue")
        with pytest.raises(PermissionError, match="organization"):
            first.update_task_status(task_id=task_id, actor="mallory", status="completed", device_id="dev_real")
        done = first.update_task_status(task_id=task_id, actor=OWNER, status="completed", result_summary="done", device_id="dev_real")
        assert done["status"] == "completed"
        with pytest.raises(ValueError, match="already completed"):
            second.update_task_status(task_id=task_id, actor=OPERATOR, status="queued")
        assert second.claim_next_task(actor=OWNER, device_id="dev_real") is None
        assert second.get_task(task_id)["result_summary"] == "done"
    finally:
        first.close()
        second.close()


def test_postgres_unserialized_persist_fails_closed() -> None:
    from omnidesk_agent.appsync.migrated_postgres_store import MigratedMultiInstancePostgresAppSyncStore

    store = MigratedMultiInstancePostgresAppSyncStore.__new__(MigratedMultiInstancePostgresAppSyncStore)
    store._operation_state = threading.local()
    with pytest.raises(RuntimeError, match="serialized operation"):
        store._persist()


def test_postgres_project_writes_do_not_roll_back_other_instances() -> None:
    from omnidesk_agent.appsync.projects import GatewayProjectStore

    first, second = _postgres_stores("stale_writeback")
    try:
        first.ensure_user(OWNER)
        first.register_device(actor=OWNER, device_id="dev_real", device_type="desktop", name="real", platform="mac")
        task_id, approval_id = _approved_desktop_task(first)
        second.bootstrap(OWNER)  # second now caches the task as queued and the approval as approved
        first.claim_next_task(actor=OWNER, device_id="dev_real")
        first.update_task_status(task_id=task_id, actor=OWNER, status="completed", result_summary="done", device_id="dev_real")
        # A project write on the instance with the stale cache must not mirror it back.
        GatewayProjectStore(second).create_project(actor=OWNER, name="Stale cache probe")
        assert first.get_task(task_id)["status"] == "completed"
        assert {a["approval_id"]: a["status"] for a in first.list_approvals(actor=OWNER)}[approval_id] == "consumed"
        assert first.claim_next_task(actor=OWNER, device_id="dev_real") is None
    finally:
        first.close()
        second.close()
