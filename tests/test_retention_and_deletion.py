"""Deleting a conversation erases what was derived from it, and the core
runtime state no longer keeps expired or aged rows forever.

Before this there was no deletion path for chat data at all, and on PostgreSQL
the memory retention purge only looked at the newest 1000 rows, so old expired
memories were never found once newer ones existed.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest

from omnidesk_agent.appsync.strict_json_store import StrictJsonAppSyncStore

ALICE = "alice-erasure"
BOB = "bob-erasure"


def _dsn() -> str:
    value = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "").strip()
    if not value:
        pytest.skip("OMNIDESK_TEST_POSTGRES_DSN is not configured")
    return value


@pytest.fixture(params=["json", "postgres"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    if request.param == "json":
        yield StrictJsonAppSyncStore(tmp_path / "appsync.json")
        return
    from omnidesk_agent.appsync.migrated_postgres_store import MigratedMultiInstancePostgresAppSyncStore
    from omnidesk_agent.appsync.postgres_migrations import apply_appsync_migrations

    namespace = f"erasure_{uuid.uuid4().hex}"
    apply_appsync_migrations(_dsn(), namespace=namespace)
    pg = MigratedMultiInstancePostgresAppSyncStore(dsn=_dsn(), namespace=namespace, pool_size=2)
    try:
        yield pg
    finally:
        pg.close()


def test_deleting_a_conversation_erases_what_was_derived_from_it(store: Any) -> None:
    store.ensure_user(ALICE)
    store.ensure_user(BOB)
    secret = store.create_conversation(actor=ALICE, title="Secret layoffs plan", idempotency_key="conv-key", idempotency_payload={"title": "Secret layoffs plan"})
    keep = store.create_conversation(actor=ALICE, title="Team lunch")
    conversation_id = secret["conversation_id"]
    store.add_chat_user_message(actor=ALICE, conversation_id=conversation_id, content="who is on the list")
    store.add_assistant_message(actor=ALICE, conversation_id=conversation_id, content="the list is ...", provider="p", model="m", profile="fast")
    task = store.add_message_and_task(actor=ALICE, conversation_id=conversation_id, content="draft the notice")["task"]
    store.add_chat_user_message(actor=ALICE, conversation_id=keep["conversation_id"], content="pizza?")
    store.receive_outbox_operations(actor=ALICE, operations=[{"operation_id": "op-1", "operation_type": "message.created", "payload": {"message_id": "msg_offline", "conversation_id": conversation_id, "content": "offline note"}}])
    head_before = store.sync_since(0)["head_seq"]

    with pytest.raises(KeyError):
        store.delete_conversation(actor=BOB, conversation_id=conversation_id)
    result = store.delete_conversation(actor=ALICE, conversation_id=conversation_id)
    deleted = result["deleted"]
    assert deleted["conversations"] == 1 and deleted["messages"] == 4  # two chat turns, the task message, the offline upload
    assert deleted["notifications"] >= 1 and deleted["idempotency_keys"] >= 1 and deleted["local_inbox"] >= 1
    assert deleted["sync_events_redacted"] >= 1

    with pytest.raises(KeyError):
        store.get_conversation(conversation_id, actor=ALICE)
    with pytest.raises(KeyError):
        store.list_messages(conversation_id, actor=ALICE)
    with pytest.raises(KeyError):
        store.delete_conversation(actor=ALICE, conversation_id=conversation_id)
    feed = store.sync_since(0, actor=ALICE)
    text = json.dumps(feed)
    assert "Secret layoffs plan" not in text and "the list is" not in text
    # Event numbers are untouched, so every reader's cursor stays valid.
    assert feed["head_seq"] == head_before + 1 and feed["events"][-1]["event_type"] == "conversation.deleted"
    assert "the list is" not in json.dumps(store.list_notifications(actor=ALICE))
    # The idempotency key no longer replays the erased conversation.
    again = store.create_conversation(actor=ALICE, title="Secret layoffs plan", idempotency_key="conv-key", idempotency_payload={"title": "Secret layoffs plan"})
    assert again["conversation_id"] != conversation_id
    # Untouched: the other conversation, and the task as the organization's record.
    assert [m["content"] for m in store.list_messages(keep["conversation_id"], actor=ALICE)] == ["pizza?"]
    assert store.get_task(task["task_id"], actor=ALICE)["status"] in {"queued", "blocked"}


def test_delete_route_scopes_to_the_caller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from omnidesk_agent.config import AppConfig
    from omnidesk_agent.server import create_app

    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", ALICE)
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
    headers = {"authorization": "Bearer operator-token", "x-omnidesk-actor": ALICE}
    with TestClient(create_app(cfg)) as client:
        created = client.post("/app/conversations", headers={**headers, "idempotency-key": "c1"}, json={"title": "to erase"}).json()["conversation"]
        response = client.delete(f"/app/conversations/{created['conversation_id']}", headers=headers)
        assert response.status_code == 200 and response.json()["deleted"]["conversations"] == 1
        assert client.delete(f"/app/conversations/{created['conversation_id']}", headers=headers).status_code == 404
        assert client.delete("/app/conversations/conv_missing", headers=headers).status_code == 404


# ------------------------------------------------------------- core state ---


@pytest.fixture
def core_state() -> Iterator[Any]:
    from omnidesk_agent.repositories.postgres_state import PostgresRuntimeStateStores

    stores = PostgresRuntimeStateStores(_dsn())
    try:
        yield stores
    finally:
        stores.state.close()


def _backdate(state: Any, namespace: str, keys: list[str], *, days: float) -> None:
    with state._connect() as con, con.cursor() as cur:
        cur.execute(
            "UPDATE omnidesk_core_state SET created_at = created_at - %s, updated_at = updated_at - %s WHERE namespace = %s AND key = ANY(%s)",
            (days * 86_400, days * 86_400, namespace, keys),
        )


def test_memory_purge_reaches_expired_rows_older_than_the_newest_thousand(core_state: Any) -> None:
    actor = f"memory-{uuid.uuid4().hex}"
    state = core_state.state
    now = time.time()
    expired = [f"{9_000_000 + n}" for n in range(20)]
    for key in expired:
        state.put("structured_experiences", key, {"id": int(key), "actor": actor, "channel": "web", "expires_at": now - 3600})
    _backdate(state, "structured_experiences", expired, days=30)
    fresh = [f"{9_100_000 + n}" for n in range(1005)]
    with state._connect() as con, con.cursor() as cur:
        cur.executemany(
            "INSERT INTO omnidesk_core_state(namespace, key, value_json, created_at, updated_at) VALUES ('structured_experiences', %s, %s::jsonb, %s, %s) ON CONFLICT DO NOTHING",
            [(key, json.dumps({"id": int(key), "actor": actor, "channel": "web", "expires_at": now + 86_400}), now, now) for key in fresh],
        )
    memory = core_state.memory_store()
    try:
        dry = memory.purge_expired(dry_run=True, actor=actor, limit=1000)
        assert dry["candidate_count"] == 20
        done = memory.purge_expired(dry_run=False, actor=actor, limit=1000)
        assert done["deleted_count"] == 20
        assert memory.purge_expired(dry_run=True, actor=actor)["candidate_count"] == 0
    finally:
        with state._connect() as con, con.cursor() as cur:
            cur.execute("DELETE FROM omnidesk_core_state WHERE namespace = 'structured_experiences' AND value_json->>'actor' = %s", (actor,))


def test_per_call_budget_count_can_use_its_index(core_state: Any) -> None:
    """count_calls runs on every model call; it must not scan the whole namespace."""
    state = core_state.state
    task_id = f"task-{uuid.uuid4().hex}"
    state.put("llm_usage", f"u-{task_id}", {"id": f"u-{task_id}", "task_id": task_id, "created_at": time.time()})
    assert state.count_by_field("llm_usage", "task_id", task_id) == 1
    with state._connect() as con, con.cursor() as cur:
        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute("EXPLAIN SELECT COUNT(*) FROM omnidesk_core_state WHERE namespace=%s AND value_json->>'task_id'=%s", ("llm_usage", task_id))
        plan = " ".join(str(row[0]) for row in cur.fetchall())
    assert "idx_omnidesk_core_state_task_id" in plan


def test_core_state_retention_prunes_expired_and_aged_rows(core_state: Any) -> None:
    from omnidesk_agent.repositories.retention import run_core_state_retention

    state = core_state.state
    now = time.time()
    tag = uuid.uuid4().hex
    state.put("llm_cache", f"old-{tag}", {"cache_key": f"old-{tag}", "response": "x", "created_at": now})
    state.put("llm_cache", f"new-{tag}", {"cache_key": f"new-{tag}", "response": "y", "created_at": now})
    _backdate(state, "llm_cache", [f"old-{tag}"], days=8)
    state.put("llm_usage", f"old-{tag}", {"id": f"old-{tag}", "task_id": tag, "created_at": now})
    state.put("llm_usage", f"new-{tag}", {"id": f"new-{tag}", "task_id": tag, "created_at": now})
    _backdate(state, "llm_usage", [f"old-{tag}"], days=91)
    state.put("side_effect_idempotency", f"dead-{tag}", {"scope_key": f"dead-{tag}", "expires_at": now - 1})
    state.put("side_effect_idempotency", f"live-{tag}", {"scope_key": f"live-{tag}", "expires_at": now + 3600})
    report = run_core_state_retention(core_state, now=now)
    assert report["llm_cache"]["deleted"] >= 1 and report["llm_usage"]["deleted"] >= 1 and report["side_effect_idempotency"]["deleted"] >= 1
    assert state.get("llm_cache", f"old-{tag}") is None and state.get("llm_cache", f"new-{tag}") is not None
    assert state.get("llm_usage", f"old-{tag}") is None and state.get("llm_usage", f"new-{tag}") is not None
    assert state.get("side_effect_idempotency", f"dead-{tag}") is None and state.get("side_effect_idempotency", f"live-{tag}") is not None


def test_core_state_is_pruned_in_the_elected_appsync_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnidesk_agent import appsync

    seen: dict[str, Any] = {}

    class _Store:
        def start_retention(self, *, interval_seconds: float, metrics: Any = None, also: Any = None) -> None:
            seen["also"] = also

    class _Repositories:
        def run_core_state_retention(self) -> dict[str, Any]:
            return {}

    monkeypatch.setenv("OMNIDESK_APPSYNC_RETENTION_INTERVAL_SECONDS", "900")
    appsync._start_retention(_Store(), None, _Repositories())
    assert set(seen["also"]) == {"core_state"}
    appsync._start_retention(_Store(), None, object())
    assert seen["also"] is None


def test_a_failing_extra_job_does_not_stop_retention() -> None:
    from omnidesk_agent.appsync.migrated_postgres_store import MigratedMultiInstancePostgresAppSyncStore
    from omnidesk_agent.appsync.postgres_migrations import apply_appsync_migrations
    from omnidesk_agent.appsync.retention import run_retention

    namespace = f"retention_jobs_{uuid.uuid4().hex}"
    apply_appsync_migrations(_dsn(), namespace=namespace)
    store = MigratedMultiInstancePostgresAppSyncStore(dsn=_dsn(), namespace=namespace, pool_size=2)
    try:
        def broken() -> dict[str, Any]:
            raise RuntimeError("core state unavailable")

        report = run_retention(store, also={"core_state": broken, "other": lambda: {"deleted": 0}})
        assert report["core_state"] == {"error": "RuntimeError"} and report["other"] == {"deleted": 0}
        assert "sync_events" in report["tables"]
    finally:
        store.close()
