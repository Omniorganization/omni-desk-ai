from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from omnidesk_agent.appsync.store import AppSyncStore
from omnidesk_agent.appsync.postgres_store import NORMALIZED_SCHEMA_SQL
from omnidesk_agent.config import AppConfig
from omnidesk_agent.server import create_app


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.runtime.offline_mode = True
    cfg.apply_runtime_policies()
    cfg.workspace.root = tmp_path / "workspace"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.learning.growth_plan_file = tmp_path / "growth.json"
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.token_file = tmp_path / "google" / "token.json"
    cfg.gateway.admin_allowed_ips = ["testclient", "127.0.0.1", "::1"]
    cfg.app_sync.allow_websocket_query_auth = True
    return cfg


def test_appsync_offline_outbox_persists_events_and_retry_state(tmp_path: Path):
    path = tmp_path / "app_sync.json"
    store = AppSyncStore(path, local_outbox_enabled=True)
    store.register_device(actor="alice", device_id="mobile-1", device_type="mobile", name="Phone", platform="iOS")
    convo = store.create_conversation(actor="alice", title="Offline task", source_device_id="mobile-1")

    pending = store.pending_local_outbox(actor="alice")
    assert pending
    assert any(item["operation_type"] == "conversation.created" for item in pending)

    failed = store.mark_local_operation_failed(pending[0]["operation_id"], error="network down", retry_delay_seconds=1)
    assert failed["status"] == "failed"
    reloaded = AppSyncStore(path, local_outbox_enabled=True)
    assert reloaded.sync_state(actor="alice")["outbox"]["pending"] >= 1
    assert any(item.payload["event_type"] == "conversation.created" for item in reloaded.local_outbox.values())
    assert convo["conversation_id"]


def test_appsync_sync_records_conflict_for_same_idempotency_different_payload(tmp_path: Path):
    store = AppSyncStore(tmp_path / "app_sync.json")
    store.ensure_user("alice")
    local = store.enqueue_local_operation(actor="alice", operation_type="task.status", payload={"task_id": "task-1", "status": "completed"}, idempotency_key="op-1")

    result = store.record_remote_events(
        actor="alice",
        events=[{"seq": 10, "event_type": "task.status", "idempotency_key": "op-1", "payload": {"task_id": "task-1", "status": "failed"}}],
    )

    assert result["conflicts"]
    assert store.local_outbox[local["operation_id"]].status == "conflict"
    assert store.sync_state(actor="alice")["conflicts"]["open"] == 1


def test_appsync_bidirectional_sync_route_accepts_outbox_operations(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", "viewer-token")
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "owner-token")
    app = create_app(_cfg(tmp_path))

    with TestClient(app) as client:
        headers = {"authorization": "Bearer operator-token", "x-omnidesk-actor": "alice"}
        payload = {
            "device_id": "mobile-1",
            "operations": [
                {
                    "operation_id": "op-1",
                    "operation_type": "conversation.created",
                    "idempotency_key": "offline-create-1",
                    "payload": {"conversation_id": "conv-1", "title": "Offline"},
                }
            ],
            "since_seq": 0,
        }
        response = client.post("/app/sync", headers=headers, json=payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["uploaded"]["applied"] == 1
    assert body["state"]["inbox"]["received"] == 1


def test_postgres_schema_exposes_offline_sync_tables():
    for table in [
        "omnidesk_appsync_local_outbox",
        "omnidesk_appsync_local_inbox",
        "omnidesk_appsync_sync_cursors",
        "omnidesk_appsync_sync_conflicts",
        "omnidesk_appsync_operation_log",
    ]:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in NORMALIZED_SCHEMA_SQL
