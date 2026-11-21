"""Offline sync: conflict policies are enforced, conflicts can be resolved, and
the daemon's outbox actually reaches the remote gateway.

Before this, a conflicting upload was recorded as a conflict *and applied*
anyway (manual-review returned conflicts=1 and applied_state_mutations=1),
nothing could resolve a conflict, open conflicts failed every reconnect, and
the daemon had no upload target at all.
"""

from __future__ import annotations

import io
import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from omnidesk_agent.appsync.store import AppSyncStore
from omnidesk_agent.appsync.strict_json_store import StrictJsonAppSyncStore
from omnidesk_agent.config import AppConfig
from omnidesk_agent.offline.connectivity import ConnectivityManager, ProbeResult, ReconnectSyncWorker
from omnidesk_agent.offline.uploader import GatewayOutboxUploader, build_outbox_uploader, validate_gateway_url
from omnidesk_agent.server import create_app

ALICE = "alice"
MALLORY = "mallory"


@pytest.fixture(params=["json", "postgres"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    if request.param == "json":
        yield StrictJsonAppSyncStore(tmp_path / "appsync.json")
        return
    dsn = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("OMNIDESK_TEST_POSTGRES_DSN is not configured")
    from omnidesk_agent.appsync.migrated_postgres_store import MigratedMultiInstancePostgresAppSyncStore
    from omnidesk_agent.appsync.postgres_migrations import apply_appsync_migrations

    namespace = f"conflicts_{uuid.uuid4().hex}"
    apply_appsync_migrations(dsn, namespace=namespace)
    pg = MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=namespace, pool_size=2)
    try:
        yield pg
    finally:
        pg.close()


def _upload(store: Any, operations: list[dict[str, Any]], *, policy: str = "manual-review", actor: str = ALICE) -> dict[str, Any]:
    return store.receive_outbox_operations(actor=actor, operations=operations, remote="laptop", device_id="dev_laptop", conflict_policy=policy)


def _conversation_op(op_id: str, title: str, **extra: Any) -> dict[str, Any]:
    return {"operation_id": op_id, "operation_type": "conversation.created", "payload": {"conversation_id": "conv_shared", "title": title, **extra}}


def _title(store: Any) -> str:
    return store.get_conversation("conv_shared", actor=ALICE)["title"]


def _statuses(result: dict[str, Any]) -> list[str]:
    return [item["status"] for item in result["operations"]]


@pytest.mark.parametrize(
    ("policy", "extra", "expected_status", "expected_title", "open_conflicts"),
    [
        ("manual-review", {}, "conflict", "Original", 1),
        ("server-wins", {}, "server_kept", "Original", 0),
        ("client-wins", {}, "client_applied", "Offline edit", 0),
        # merge: only the client changed since its base, so the client's edit lands.
        ("merge", {"base_title": "Original"}, "merged", "Offline edit", 0),
        # merge: the client did not change the field; the server's value stays.
        ("merge", {"base_title": "Offline edit"}, "server_kept", "Original", 0),
        # merge: both sides changed since the base; a person decides.
        ("merge", {"base_title": "Something else"}, "conflict", "Original", 1),
        ("merge", {}, "conflict", "Original", 1),
    ],
)
def test_conflict_policy_decides_a_diverged_record(store: Any, policy: str, extra: dict[str, Any], expected_status: str, expected_title: str, open_conflicts: int) -> None:
    store.ensure_user(ALICE)
    assert _statuses(_upload(store, [_conversation_op("op-1", "Original")])) == ["applied"]
    result = _upload(store, [_conversation_op("op-2", "Offline edit", **extra)], policy=policy)
    assert _statuses(result) == [expected_status]
    assert _title(store) == expected_title
    assert result["applied_state_mutations"] == (1 if expected_status in {"client_applied", "merged"} else 0)
    assert len(store.list_sync_conflicts(actor=ALICE)) == open_conflicts
    if expected_status != "conflict":
        assert [c["status"] for c in store.list_sync_conflicts(actor=ALICE, status="resolved")] == ["resolved"]


def test_messages_are_append_only_whatever_the_policy(store: Any) -> None:
    store.ensure_user(ALICE)
    message = {"operation_id": "op-m1", "operation_type": "message.created", "payload": {"message_id": "msg_1", "conversation_id": "conv_chat", "content": "Original words"}}
    assert _statuses(_upload(store, [message])) == ["applied"]
    rewrite = {**message, "operation_id": "op-m2", "payload": {**message["payload"], "content": "Rewritten history"}}
    assert _statuses(_upload(store, [rewrite], policy="client-wins")) == ["conflict"]
    assert [m["content"] for m in store.list_messages("conv_chat", actor=ALICE)] == ["Original words"]
    assert _statuses(_upload(store, [{**rewrite, "operation_id": "op-m3"}], policy="server-wins")) == ["server_kept"]


def test_approved_or_desktop_tasks_are_never_rewritten_by_upload(store: Any) -> None:
    store.ensure_user(ALICE)
    conversation = store.create_conversation(actor=ALICE, title="ops")
    created = store.add_message_and_task(actor=ALICE, conversation_id=conversation["conversation_id"], content="deploy to prod", requires_desktop_runtime=True)
    task_id = created["task"]["task_id"]
    forged = {"operation_id": "op-t1", "operation_type": "task.created", "payload": {"task_id": task_id, "conversation_id": conversation["conversation_id"], "title": "rm -rf everything"}}
    result = _upload(store, [forged], policy="client-wins")
    assert _statuses(result) == ["conflict"]
    assert store.get_task(task_id, actor=ALICE)["title"] == "deploy to prod"
    conflict_id = result["operations"][0]["conflict_id"]
    with pytest.raises(PermissionError):
        store.resolve_sync_conflict(actor=ALICE, conflict_id=conflict_id, resolution="apply_client")
    assert store.get_task(task_id, actor=ALICE)["title"] == "deploy to prod"


def test_a_retried_upload_is_a_duplicate_not_a_conflict(store: Any) -> None:
    store.ensure_user(ALICE)
    first = _conversation_op("op-1", "Original")
    assert _statuses(_upload(store, [first])) == ["applied"]
    assert _statuses(_upload(store, [_conversation_op("op-2", "Changed elsewhere")], policy="client-wins")) == ["client_applied"]
    retried = _upload(store, [first])
    assert _statuses(retried) == ["duplicate"] and retried["conflicts"] == []
    assert _title(store) == "Changed elsewhere"


def test_another_actors_record_is_rejected_without_a_conflict(store: Any) -> None:
    store.ensure_user(ALICE)
    store.ensure_user(MALLORY)
    assert _statuses(_upload(store, [_conversation_op("op-1", "Alice's private chat")])) == ["applied"]
    hijack = _upload(store, [_conversation_op("op-x", "Mallory was here")], policy="client-wins", actor=MALLORY)
    assert _statuses(hijack) == ["rejected"] and hijack["conflicts"] == []
    assert _title(store) == "Alice's private chat"
    assert store.list_sync_conflicts(actor=MALLORY) == [] and store.list_sync_conflicts(actor=ALICE) == []


@pytest.mark.parametrize(("resolution", "payload", "expected"), [("keep_server", None, "Original"), ("apply_client", None, "Offline edit"), ("merged", {"title": "Both edits"}, "Both edits")])
def test_the_actor_resolves_an_open_conflict(store: Any, resolution: str, payload: dict[str, Any] | None, expected: str) -> None:
    store.ensure_user(ALICE)
    store.ensure_user(MALLORY)
    _upload(store, [_conversation_op("op-1", "Original")])
    conflict_id = _upload(store, [_conversation_op("op-2", "Offline edit")])["operations"][0]["conflict_id"]
    with pytest.raises(KeyError):
        store.resolve_sync_conflict(actor=MALLORY, conflict_id=conflict_id, resolution="apply_client")
    resolved = store.resolve_sync_conflict(actor=ALICE, conflict_id=conflict_id, resolution=resolution, payload=payload)
    assert resolved["status"] == "resolved" and resolved["resolved_at"]
    assert _title(store) == expected
    assert store.list_sync_conflicts(actor=ALICE) == []
    with pytest.raises(ValueError, match="already resolved"):
        store.resolve_sync_conflict(actor=ALICE, conflict_id=conflict_id, resolution="keep_server")


def test_local_outbox_conflicts_follow_the_policy_too(tmp_path: Path) -> None:
    """A remote event that diverges from a queued local operation."""
    for policy, expected_outbox, open_count in (("server-wins", "synced", 0), ("client-wins", "pending", 0), ("manual-review", "conflict", 1)):
        store = AppSyncStore(tmp_path / f"{policy}.json")
        store.ensure_user(ALICE)
        local = store.enqueue_local_operation(actor=ALICE, operation_type="task.status", payload={"task_id": "t1", "status": "completed"}, idempotency_key="op-1")
        store.record_remote_events(actor=ALICE, events=[{"seq": 3, "event_type": "task.status", "idempotency_key": "op-1", "payload": {"task_id": "t1", "status": "failed"}}], conflict_policy=policy)
        assert store.local_outbox[local["operation_id"]].status == expected_outbox, policy
        assert store.sync_state(actor=ALICE)["conflicts"]["open"] == open_count, policy


# ------------------------------------------------------------------ routes ---


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
    return cfg


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", ALICE)
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "owner-token")
    with TestClient(create_app(_cfg(tmp_path))) as client:
        yield client


OPERATOR = {"authorization": "Bearer operator-token", "x-omnidesk-actor": ALICE}


def test_sync_routes_hold_and_resolve_conflicts(gateway: TestClient) -> None:
    body = {"device_id": "dev_laptop", "operations": [_conversation_op("op-1", "Original")]}
    assert gateway.post("/app/sync", headers=OPERATOR, json=body).status_code == 200
    held = gateway.post("/app/sync", headers=OPERATOR, json={**body, "operations": [_conversation_op("op-2", "Offline edit")], "conflict_policy": "manual-review"})
    assert held.status_code == 200, held.text
    uploaded = held.json()["uploaded"]
    assert [item["status"] for item in uploaded["operations"]] == ["conflict"]
    assert uploaded["applied_state_mutations"] == 0
    conflict_id = uploaded["operations"][0]["conflict_id"]
    listed = gateway.get("/app/sync/conflicts", headers=OPERATOR).json()["conflicts"]
    assert [c["conflict_id"] for c in listed] == [conflict_id]
    resolved = gateway.post(f"/app/sync/conflicts/{conflict_id}/resolve", headers=OPERATOR, json={"resolution": "apply_client"})
    assert resolved.status_code == 200 and resolved.json()["conflict"]["status"] == "resolved"
    assert gateway.post(f"/app/sync/conflicts/{conflict_id}/resolve", headers=OPERATOR, json={"resolution": "keep_server"}).status_code == 409
    assert gateway.post("/app/sync/conflicts/conflict_missing/resolve", headers=OPERATOR, json={"resolution": "keep_server"}).status_code == 404
    assert gateway.post(f"/app/sync/conflicts/{conflict_id}/resolve", headers=OPERATOR, json={"resolution": "nuke"}).status_code == 422


# ----------------------------------------------------- daemon reconnect ---


def _online() -> ConnectivityManager:
    return ConnectivityManager(probes=[lambda: ProbeResult("dns", True, "ok")])


def _offline_node(tmp_path: Path, name: str = "node.json") -> AppSyncStore:
    node = AppSyncStore(tmp_path / name)
    node.ensure_user(ALICE)
    return node


def test_reconnect_marks_each_operation_by_its_outcome_and_does_not_fail_on_conflicts(tmp_path: Path) -> None:
    node = _offline_node(tmp_path)
    ops = [node.enqueue_local_operation(actor=ALICE, operation_type="conversation.created", payload={"n": i}, idempotency_key=f"k{i}") for i in range(3)]
    outcomes = {ops[0]["operation_id"]: "applied", ops[1]["operation_id"]: "conflict", ops[2]["operation_id"]: "rejected"}
    calls: list[str] = []

    def upload(items: list[dict[str, Any]]) -> dict[str, Any]:
        calls.append("upload")
        return {"ok": True, "operations": [{"operation_id": item["operation_id"], "status": outcomes[item["operation_id"]], "conflict_id": "conflict_remote"} for item in items]}

    def update_check() -> dict[str, Any]:
        calls.append("update")
        return {"ok": True}

    result = ReconnectSyncWorker(connectivity=_online(), store=node, actor=ALICE, upload=upload, update_check=update_check).run_once()
    assert result["ok"] is True and result["state"] == "healthy"
    assert (result["synced"], result["rejected"], result["held_conflicts"]) == (1, 1, ["conflict_remote"])
    assert [node.local_outbox[op["operation_id"]].status for op in ops] == ["synced", "conflict", "rejected"]
    assert "conflict_remote" in (node.local_outbox[ops[1]["operation_id"]].last_error or "")
    assert calls == ["upload", "update"]
    # Held and rejected operations are not sent again on the next pass.
    assert node.pending_local_outbox(actor=ALICE) == []


def test_open_local_conflicts_defer_updates_instead_of_failing_sync(tmp_path: Path) -> None:
    node = _offline_node(tmp_path)
    node.enqueue_local_operation(actor=ALICE, operation_type="task.status", payload={"status": "completed"}, idempotency_key="op-1")
    node.record_remote_events(actor=ALICE, events=[{"seq": 1, "idempotency_key": "op-1", "payload": {"status": "failed"}}])
    calls: list[str] = []
    result = ReconnectSyncWorker(connectivity=_online(), store=node, actor=ALICE, upload=lambda items: {"ok": True}, update_check=lambda: calls.append("update") or {"ok": True}).run_once()
    assert result["ok"] is True and result["state"] == "healthy"
    assert result["update"]["status"] == "deferred" and calls == []


def test_uploader_refuses_plain_http_to_a_remote_host() -> None:
    with pytest.raises(ValueError, match="https"):
        validate_gateway_url("http://gateway.example.test")
    with pytest.raises(ValueError, match="credentials"):
        validate_gateway_url("https://user:pass@gateway.example.test")
    assert validate_gateway_url("https://gateway.example.test/") == "https://gateway.example.test"
    assert validate_gateway_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"


def test_uploader_is_configured_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = AppConfig()
    monkeypatch.delenv("OMNIDESK_SYNC_UPLOAD_TOKEN", raising=False)
    monkeypatch.delenv("OMNIDESK_SYNC_UPLOAD_URL", raising=False)
    assert build_outbox_uploader(cfg) is None
    monkeypatch.setenv("OMNIDESK_SYNC_UPLOAD_URL", "https://gateway.example.test")
    monkeypatch.setenv("OMNIDESK_SYNC_UPLOAD_TOKEN", "t0ken")
    monkeypatch.setenv("OMNIDESK_SYNC_UPLOAD_ACTOR", ALICE)
    uploader = build_outbox_uploader(cfg)
    assert uploader is not None and uploader.actor == ALICE and uploader.base_url == "https://gateway.example.test"


class _GatewayOpener:
    """Routes the uploader's urllib requests into a TestClient (the remote gateway)."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: float) -> Any:
        self.requests.append(request)
        response = self.client.post(urlsplit(request.full_url).path, headers=dict(request.header_items()), content=request.data)
        assert response.status_code == 200, response.text
        return _Response(response.content)


class _Response(io.BytesIO):
    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def test_offline_outbox_reaches_the_remote_gateway_end_to_end(tmp_path: Path, gateway: TestClient) -> None:
    """Node outbox → reconnect worker → uploader → remote POST /app/sync → per-operation marks."""
    node = AppSyncStore(tmp_path / "node.json", local_outbox_enabled=True)
    node.ensure_user(ALICE)
    node.ensure_user("bob")
    mine = node.enqueue_local_operation(actor=ALICE, operation_type="conversation.created", payload={"conversation_id": "conv_offline", "title": "Captured offline"}, idempotency_key="c1")
    clash = node.enqueue_local_operation(actor=ALICE, operation_type="conversation.created", payload={"conversation_id": "conv_offline", "title": "Edited offline"}, idempotency_key="c2")
    theirs = node.enqueue_local_operation(actor="bob", operation_type="conversation.created", payload={"conversation_id": "conv_bob", "title": "Bob's"}, idempotency_key="b1")
    opener = _GatewayOpener(gateway)
    uploader = GatewayOutboxUploader(base_url="http://127.0.0.1", token="operator-token", actor=ALICE, opener=opener)
    result = ReconnectSyncWorker(connectivity=_online(), store=node, actor=ALICE, upload=uploader).run_once()

    assert result["ok"] is True
    statuses = {item.operation_id: item.status for item in node.local_outbox.values() if item.operation_id in {mine["operation_id"], clash["operation_id"], theirs["operation_id"]}}
    assert statuses[mine["operation_id"]] == "synced"
    assert statuses[clash["operation_id"]] == "conflict"  # held at the gateway for review
    assert statuses[theirs["operation_id"]] == "failed"  # never published under Alice's token; retried later
    sent = [op["operation_id"] for op in json.loads(opener.requests[0].data)["operations"]]
    assert theirs["operation_id"] not in sent and sent.index(mine["operation_id"]) < sent.index(clash["operation_id"])
    # Events this node queued on its own (user.created) are not uploadable: rejected once, never retried.
    auto = [item for item in node.local_outbox.values() if item.operation_type == "user.created" and item.actor == ALICE]
    assert auto and {item.status for item in auto} == {"rejected"}
    assert {item["operation_id"] for item in node.pending_local_outbox(actor=ALICE)} <= {theirs["operation_id"]}
    assert opener.requests[0].get_header("Authorization") == "Bearer operator-token"
    remote = gateway.get("/app/conversations", headers=OPERATOR).json()["conversations"]
    assert [c["title"] for c in remote if c["conversation_id"] == "conv_offline"] == ["Captured offline"]
    held = gateway.get("/app/sync/conflicts", headers=OPERATOR).json()["conflicts"]
    assert [c["remote_event_id"] for c in held] == [clash["operation_id"]]


def test_audit_probe_manual_review_no_longer_applies_the_conflicting_upload(store: Any) -> None:
    """The audit's probe: an upload reusing a queued operation's idempotency key with
    another payload was recorded as a conflict and applied anyway."""
    store.ensure_user(ALICE)
    store.enqueue_local_operation(actor=ALICE, operation_type="conversation.created", payload={"conversation_id": "conv_probe", "title": "Queued here"}, idempotency_key="k-1")
    upload = {"operation_id": "op-probe", "operation_type": "conversation.created", "idempotency_key": "k-1", "payload": {"conversation_id": "conv_probe", "title": "Uploaded elsewhere"}}
    result = _upload(store, [upload], policy="manual-review")
    assert len(result["conflicts"]) == 1
    assert result["applied_state_mutations"] == 0
    assert _statuses(result) == ["conflict"] and result["operations"][0]["conflict_id"] == result["conflicts"][0]["conflict_id"]
    with pytest.raises(KeyError):
        store.get_conversation("conv_probe", actor=ALICE)
