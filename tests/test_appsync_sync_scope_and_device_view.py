"""Regression tests for AppSync low findings L1 and L4 of the 2026-10-21 review.

L1: ``record_remote_events`` matched queued offline operations by idempotency key
alone, so any operator could mark another organization's queued operation as a
conflict (or as synced), and pre-claim its remote event ids. L4: ``bootstrap``
filtered devices by the stored profile role, which is always ``operator``, so a
viewer saw every device in the organization together with its push target and
token hash. ``test_outbox_crossorg_poc_is_rejected`` replays the review's proof
of concept.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omnidesk_agent.appsync.store import AppSyncStore, public_device_view
from omnidesk_agent.config import AppConfig
from omnidesk_agent.server import create_app

CREDENTIAL_FIELDS = {"push_token", "token_hash", "last_challenge_nonce_hash", "last_challenge_expires_at"}
VICTIM_PUSH = "fid:" + "V" * 18 + "vpsh"
OTHER_PUSH = "fid:" + "O" * 18 + "opsh"


def _queued(store: AppSyncStore, *, actor: str, operation_type: str = "conversation.created"):
    return [item for item in store.local_outbox.values() if item.actor == actor and item.operation_type == operation_type]


def _offline_store(tmp_path: Path) -> AppSyncStore:
    store = AppSyncStore(tmp_path / "appsync.json", local_outbox_enabled=True)
    store.ensure_user("victim", organization_id="org_a")
    store.ensure_user("mallory", organization_id="org_b")
    return store


# --- L1: offline outbox matching is scoped to the caller ----------------------


def test_outbox_crossorg_poc_is_rejected(tmp_path: Path) -> None:
    store = _offline_store(tmp_path)
    store.create_conversation(actor="victim", title="t")
    [op] = _queued(store, actor="victim")
    assert op.status == "pending"

    result = store.record_remote_events(actor="mallory", events=[{"seq": 1, "event_id": "x1", "idempotency_key": op.idempotency_key, "payload": {"junk": 1}}])

    assert result["conflicts"] == []
    assert store.local_outbox[op.operation_id].status == "pending"
    assert store.sync_conflicts == {}
    assert store.sync_state(actor="victim")["outbox"]["conflict"] == 0
    # The same echo with the original payload cannot mark it synced either.
    original = op.payload["payload"]
    store.record_remote_events(actor="mallory", events=[{"seq": 2, "event_id": "x2", "idempotency_key": op.idempotency_key, "payload": original}])
    assert store.local_outbox[op.operation_id].status == "pending"


def test_outbox_matching_requires_the_same_actor(tmp_path: Path) -> None:
    store = AppSyncStore(tmp_path / "appsync.json")
    store.ensure_user("alice")
    store.ensure_user("bob")
    local = store.enqueue_local_operation(actor="alice", operation_type="task.status", payload={"task_id": "t1", "status": "completed"}, idempotency_key="op-1")

    result = store.record_remote_events(actor="bob", events=[{"seq": 1, "idempotency_key": "op-1", "payload": {"task_id": "t1", "status": "failed"}}])

    assert result["conflicts"] == []
    assert store.local_outbox[local["operation_id"]].status == "pending"


def test_own_outbox_operations_still_sync_and_conflict(tmp_path: Path) -> None:
    store = _offline_store(tmp_path)
    store.create_conversation(actor="victim", title="first")
    store.create_conversation(actor="victim", title="second")
    first, second = sorted(_queued(store, actor="victim"), key=lambda item: item.created_at)

    synced = store.record_remote_events(actor="victim", events=[{"seq": 5, "event_id": "e1", "idempotency_key": first.idempotency_key, "payload": first.payload["payload"]}])
    conflicted = store.record_remote_events(actor="victim", events=[{"seq": 6, "event_id": "e2", "idempotency_key": second.idempotency_key, "payload": {"title": "edited elsewhere"}}])

    assert synced["applied"] == 1 and store.local_outbox[first.operation_id].status == "synced"
    assert len(conflicted["conflicts"]) == 1 and store.local_outbox[second.operation_id].status == "conflict"
    assert conflicted["conflicts"][0]["remote_event_id"] == "e2"


def test_remote_event_ids_are_deduplicated_per_caller(tmp_path: Path) -> None:
    store = _offline_store(tmp_path)
    store.create_conversation(actor="victim", title="t")
    [op] = _queued(store, actor="victim")
    event = {"seq": 3, "event_id": "evt-1", "idempotency_key": op.idempotency_key, "payload": op.payload["payload"]}

    # Another organization pre-claims the victim's event id ...
    assert store.record_remote_events(actor="mallory", events=[event])["applied"] == 1
    # ... which neither suppresses the victim's event nor gets overwritten by it.
    victim = store.record_remote_events(actor="victim", events=[event])
    assert (victim["applied"], victim["duplicates"]) == (1, 0)
    assert store.local_outbox[op.operation_id].status == "synced"
    assert store.record_remote_events(actor="victim", events=[event])["duplicates"] == 1
    owners = sorted((item.organization_id, item.actor, item.status) for item in store.local_inbox.values())
    assert owners == [("org_a", "victim", "duplicate"), ("org_b", "mallory", "applied")]
    assert len({item.remote_event_id for item in store.local_inbox.values()}) == 2


def test_inbox_records_the_authenticated_actor(tmp_path: Path) -> None:
    store = _offline_store(tmp_path)
    store.receive_outbox_operations(actor="mallory", operations=[{"operation_id": "op-x", "actor": "victim", "operation_type": "note", "payload": {}}])
    [record] = store.local_inbox.values()
    assert (record.actor, record.organization_id) == ("mallory", "org_b")


def test_scoped_inbox_survives_a_json_reload(tmp_path: Path) -> None:
    store = _offline_store(tmp_path)
    event = {"seq": 1, "event_id": "evt-1", "payload": {}}
    assert store.record_remote_events(actor="victim", events=[event])["applied"] == 1
    assert store.record_remote_events(actor="mallory", events=[event])["applied"] == 1

    reloaded = AppSyncStore(tmp_path / "appsync.json", local_outbox_enabled=True)
    assert len(reloaded.local_inbox) == 2
    assert reloaded.record_remote_events(actor="victim", events=[event])["duplicates"] == 1
    assert reloaded.record_remote_events(actor="mallory", events=[event])["duplicates"] == 1


def test_postgres_mirror_keeps_each_callers_inbox_row(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The Postgres inbox key is (namespace, remote_event_id): scoped ids keep two
    organizations' rows for the same remote event apart."""
    from omnidesk_agent.appsync.postgres_offline_sync import DurablePostgresAppSyncStore
    from omnidesk_agent.appsync.postgres_store import PostgresAppSyncStore

    source = _offline_store(tmp_path)
    event = {"seq": 1, "event_id": "evt-1", "payload": {}}
    source.record_remote_events(actor="victim", events=[event])
    source.record_remote_events(actor="mallory", events=[event])

    mirror = DurablePostgresAppSyncStore.__new__(DurablePostgresAppSyncStore)
    mirror.__dict__.update({name: getattr(source, name) for name in ("local_outbox", "local_inbox", "sync_cursors", "sync_conflicts", "operation_log")})
    mirror.namespace = "test"
    monkeypatch.setattr(PostgresAppSyncStore, "_mirror_normalized", lambda self, cur, Jsonb: None)
    executed: list[tuple[str, Any]] = []

    class _Cursor:
        def execute(self, sql: str, params: Any = None) -> None:
            executed.append((sql, params))

    mirror._mirror_normalized(_Cursor(), lambda value: value)
    inbox_rows = [params for sql, params in executed if "INSERT INTO omnidesk_appsync_local_inbox" in sql]
    assert sorted((row[1], row[5]) for row in inbox_rows) == [("org_a", "victim"), ("org_b", "mallory")]
    assert len({row[2] for row in inbox_rows}) == 2


def test_postgres_store_inherits_the_scoped_matching() -> None:
    from omnidesk_agent.appsync.migrated_postgres_store import MigratedMultiInstancePostgresAppSyncStore

    for name in ("record_remote_events", "receive_outbox_operations", "bootstrap"):
        assert getattr(MigratedMultiInstancePostgresAppSyncStore, name).__qualname__.startswith(("AppSyncStore.", "ApplyingAppSyncMixin."))


# --- L4: device lists follow the authenticated role and carry no credentials ---


def _hmac_sign(secret: str, message: str) -> str:
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def _enroll(store: AppSyncStore, *, owner: str, actor: str, device_id: str, secret: str, push_token: str) -> None:
    public_key = f"legacy-hmac:{secret}"
    store.register_device(actor=actor, device_id=device_id, device_type="mobile", name=device_id, platform="test", public_key=public_key, push_token=push_token)
    enrollment = store.start_device_enrollment(actor=owner, device_type="mobile", pairing_code="pairing-code-123")
    store.complete_device_enrollment(actor=actor, enrollment_id=enrollment["enrollment_id"], pairing_code="pairing-code-123", device_id=device_id, public_key=public_key)
    challenge = store.issue_device_challenge(actor=actor, enrollment_id=enrollment["enrollment_id"], device_id=device_id)
    store.verify_device_challenge(actor=actor, enrollment_id=enrollment["enrollment_id"], challenge_id=challenge["challenge_id"], device_id=device_id, signature=_hmac_sign(secret, challenge["signing_message"]))


def _assert_no_credentials(store: AppSyncStore, payload: Any) -> None:
    text = json.dumps(payload)
    for device in store.devices.values():
        legacy_secret = device.public_key if (device.public_key or "").startswith("legacy-hmac:") else None
        for value in (device.push_token, device.token_hash, device.last_challenge_nonce_hash, legacy_secret):
            if value:
                assert value not in text


@pytest.fixture()
def device_store(tmp_path: Path) -> AppSyncStore:
    store = AppSyncStore(tmp_path / "appsync.json")
    store.ensure_user("olivia")
    store.ensure_user("vera")
    store.ensure_user("mallory", organization_id="org_other")
    _enroll(store, owner="olivia", actor="olivia", device_id="dev_owner", secret="k1", push_token=OTHER_PUSH)
    store.register_device(actor="vera", device_id="dev_viewer", device_type="mobile", name="v", platform="ios", push_token=VICTIM_PUSH, token_hash="vera-token-hash")
    store.register_device(actor="mallory", device_id="dev_foreign", device_type="mobile", name="m", platform="ios", push_token="fcm-foreign-token-123456")
    return store


def test_viewer_bootstrap_lists_only_its_own_devices(device_store: AppSyncStore) -> None:
    # Without an authenticated role the snapshot is the viewer one.
    assert [d["device_id"] for d in device_store.bootstrap("vera")["devices"]] == ["dev_viewer"]
    snapshot = device_store.bootstrap("vera", role="viewer")
    assert [d["device_id"] for d in snapshot["devices"]] == ["dev_viewer"]
    assert snapshot["user"]["role"] == "viewer"


@pytest.mark.parametrize("role", ["operator", "owner"])
def test_operator_and_owner_bootstrap_list_the_organization_without_credentials(device_store: AppSyncStore, role: str) -> None:
    snapshot = device_store.bootstrap("vera", role=role)
    devices = {d["device_id"]: d for d in snapshot["devices"]}
    assert set(devices) == {"dev_owner", "dev_viewer"}
    for device in devices.values():
        assert not CREDENTIAL_FIELDS & set(device)
    assert devices["dev_owner"]["credential_status"] == "verified"
    assert devices["dev_owner"]["push_target_hint"] == "fid:***opsh"
    assert devices["dev_owner"]["public_key"] == "legacy-hmac:***"
    _assert_no_credentials(device_store, snapshot)


def test_public_device_view_masks_the_push_target() -> None:
    assert public_device_view({"device_id": "d", "push_token": None})["push_target_hint"] is None
    assert public_device_view({"push_token": "short"})["push_target_hint"] == "fcm_token:***"
    assert public_device_view({"push_token": "fcm-registration-token-wxyz"})["push_target_hint"] == "fcm_token:***wxyz"
    view = public_device_view({"device_id": "d", "token_hash": "h", "push_token": VICTIM_PUSH, "device_token": "kept"})
    assert view == {"device_id": "d", "device_token": "kept", "push_target_hint": "fid:***vpsh"}
    assert public_device_view({"public_key": "legacy-hmac:shared-secret"})["public_key"] == "legacy-hmac:***"
    assert public_device_view({"public_key": "base64:" + "A" * 43 + "="})["public_key"] == "base64:" + "A" * 43 + "="


# --- Routes ------------------------------------------------------------------


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


OWNER_AUTH = {"authorization": "Bearer owner-token"}
OPERATOR_AUTH = {"authorization": "Bearer operator-token"}
VIEWER_AUTH = {"authorization": "Bearer viewer-token"}


@pytest.fixture()
def gateway_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for role, actor in (("OWNER", "olivia"), ("OPERATOR", "oscar"), ("VIEWER", "vera")):
        monkeypatch.setenv(f"OMNIDESK_{role}_TOKEN", f"{role.lower()}-token")
        monkeypatch.setenv(f"OMNIDESK_{role}_ACTOR", actor)
    clients: list[TestClient] = []

    def build(configure=None):
        cfg = _cfg(tmp_path)
        if configure is not None:
            configure(cfg)
        app = create_app(cfg)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client, app

    yield build
    for client in clients:
        client.__exit__(None, None, None)


def _offline_two_orgs(cfg: AppConfig) -> None:
    cfg.runtime.offline_mode = True
    cfg.apply_runtime_policies()
    cfg.app_sync.actor_organizations = {"olivia": "org-a", "oscar": "org-b"}


def test_route_sync_cannot_touch_another_organizations_outbox(gateway_factory) -> None:
    client, app = gateway_factory(_offline_two_orgs)
    created = client.post("/app/conversations", headers={**OWNER_AUTH, "idempotency-key": "c1"}, json={"title": "olivia offline"})
    assert created.status_code == 200, created.text
    store = app.state.runtime.app_sync
    [op] = _queued(store, actor="olivia")

    synced = client.post("/app/sync", headers=OPERATOR_AUTH, json={"remote_events": [{"seq": 1, "event_id": "x1", "idempotency_key": op.idempotency_key, "payload": {"junk": 1}}]})

    assert synced.status_code == 200, synced.text
    assert synced.json()["pulled"]["conflicts"] == []
    assert store.local_outbox[op.operation_id].status == "pending"
    owner_state = client.get("/app/sync/state", headers=OWNER_AUTH).json()
    assert owner_state["outbox"]["conflict"] == 0 and owner_state["conflicts"]["open"] == 0


def test_route_bootstrap_uses_the_authenticated_role(gateway_factory) -> None:
    client, app = gateway_factory()
    store = app.state.runtime.app_sync
    store.ensure_user("vera")
    _enroll(store, owner="olivia", actor="olivia", device_id="dev_owner", secret="k1", push_token=OTHER_PUSH)
    store.register_device(actor="vera", device_id="dev_viewer", device_type="mobile", name="v", platform="ios", push_token=VICTIM_PUSH)

    viewer = client.get("/app/bootstrap", headers=VIEWER_AUTH)
    assert viewer.status_code == 200, viewer.text
    assert [d["device_id"] for d in viewer.json()["devices"]] == ["dev_viewer"]
    assert viewer.json()["user"]["role"] == "viewer"
    operator = client.get("/app/bootstrap", headers=OPERATOR_AUTH)
    assert operator.status_code == 200, operator.text
    assert {d["device_id"] for d in operator.json()["devices"]} == {"dev_owner", "dev_viewer"}
    for response in (viewer, operator):
        assert not any(CREDENTIAL_FIELDS & set(d) for d in response.json()["devices"])
        _assert_no_credentials(store, response.json())


def test_route_device_responses_carry_no_credentials(gateway_factory) -> None:
    client, app = gateway_factory()
    store = app.state.runtime.app_sync
    body = {"device_id": "dev-" + uuid.uuid4().hex, "device_type": "mobile", "name": "Phone", "platform": "ios", "push_token": "fcm-registration-token-abcd", "token_hash": "client-chosen-hash"}
    device_id = body["device_id"]

    responses = [client.post("/app/devices/register", headers={**OPERATOR_AUTH, "idempotency-key": "r1"}, json=body)]
    responses.append(client.post("/app/devices/register", headers={**OPERATOR_AUTH, "idempotency-key": "r1"}, json=body))  # idempotent replay
    responses.append(client.post(f"/app/devices/{device_id}/push-token", headers=OPERATOR_AUTH, json={"push_token": "fcm-registration-token-efgh"}))
    rotated = client.post(f"/app/devices/{device_id}/rotate-token", headers=OPERATOR_AUTH)
    responses.append(rotated)
    responses.append(client.post(f"/app/devices/{device_id}/revoke", headers=OWNER_AUTH, json={"reason": "lost"}))

    for response in responses:
        assert response.status_code == 200, response.text
        device = response.json()["device"]
        assert device["device_id"] == device_id
        assert not CREDENTIAL_FIELDS & set(device)
        assert "client-chosen-hash" not in response.text and "fcm-registration-token" not in response.text
    assert responses[2].json()["device"]["push_target_hint"] == "fcm_token:***efgh"
    assert rotated.json()["device"]["device_token"]
    assert store.devices[device_id].token_hash  # still stored, just never returned


def test_route_approval_key_response_carries_no_credentials(gateway_factory) -> None:
    client, app = gateway_factory()
    store = app.state.runtime.app_sync
    _enroll(store, owner="olivia", actor="oscar", device_id="dev_phone", secret="k2", push_token=VICTIM_PUSH)
    approval_key = "base64:" + base64.b64encode(bytes(range(32))).decode()

    response = client.post("/app/devices/dev_phone/approval-key", headers=OPERATOR_AUTH, json={"public_key": approval_key})

    assert response.status_code == 200, response.text
    device = response.json()["device"]
    assert device["approval_public_key"] == approval_key
    assert not CREDENTIAL_FIELDS & set(device)
    assert device["public_key"] == "legacy-hmac:***"
    _assert_no_credentials(store, response.json())


# --- PostgreSQL parity (runs where OMNIDESK_TEST_POSTGRES_DSN is set) --------


def _postgres_stores(prefix: str):
    dsn = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("OMNIDESK_TEST_POSTGRES_DSN is not configured")
    from omnidesk_agent.appsync.migrated_postgres_store import MigratedMultiInstancePostgresAppSyncStore
    from omnidesk_agent.appsync.postgres_migrations import apply_appsync_migrations

    namespace = f"{prefix}_{uuid.uuid4().hex}"
    apply_appsync_migrations(dsn, namespace=namespace)
    return (
        MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=namespace, local_outbox_enabled=True, pool_size=2),
        MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=namespace, local_outbox_enabled=True, pool_size=2),
    )


def test_postgres_offline_sync_is_scoped_to_the_caller() -> None:
    first, second = _postgres_stores("outbox_scope")
    try:
        org_a, org_b = f"org_a_{uuid.uuid4().hex}", f"org_b_{uuid.uuid4().hex}"
        first.ensure_user("victim", organization_id=org_a)
        first.ensure_user("mallory", organization_id=org_b)
        payload = {"task_id": "t1", "status": "completed"}
        op = first.enqueue_local_operation(actor="victim", operation_type="task.status", payload=payload, idempotency_key="op-1")

        result = second.record_remote_events(actor="mallory", events=[{"seq": 1, "event_id": "evt-1", "idempotency_key": "op-1", "payload": {"junk": 1}}])
        assert result["conflicts"] == []
        victim = first.record_remote_events(actor="victim", events=[{"seq": 1, "event_id": "evt-1", "idempotency_key": "op-1", "payload": payload}])
        assert (victim["applied"], victim["duplicates"]) == (1, 0)
        assert first.sync_state(actor="victim")["outbox"]["conflict"] == 0
        assert second.record_remote_events(actor="mallory", events=[{"seq": 1, "event_id": "evt-1", "payload": {}}])["duplicates"] == 1
        with first._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM omnidesk_appsync_local_outbox WHERE namespace=%s AND operation_id=%s", (first.namespace, op["operation_id"]))
            assert cur.fetchone() == ("synced",)
            cur.execute("SELECT organization_id, actor FROM omnidesk_appsync_local_inbox WHERE namespace=%s ORDER BY organization_id", (first.namespace,))
            assert cur.fetchall() == [(org_a, "victim"), (org_b, "mallory")]
    finally:
        first.close()
        second.close()


def test_postgres_bootstrap_hides_credentials_from_viewers() -> None:
    first, second = _postgres_stores("device_view")
    try:
        first.ensure_user("olivia")
        first.ensure_user("vera")
        _enroll(first, owner="olivia", actor="olivia", device_id="dev_owner", secret="k1", push_token=OTHER_PUSH)
        viewer = second.bootstrap("vera", role="viewer")
        operator = second.bootstrap("vera", role="operator")
        assert viewer["devices"] == []
        assert [d["device_id"] for d in operator["devices"]] == ["dev_owner"]
        assert not CREDENTIAL_FIELDS & set(operator["devices"][0])
        assert OTHER_PUSH not in json.dumps(operator)
    finally:
        first.close()
        second.close()
