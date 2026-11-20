"""Incremental sync, retention, and keeping database waits off the event loop.

A lagging or offline client must page forward through every event it has not
seen, or be told explicitly to re-bootstrap; it must never be moved past events
silently. Retention keeps tables bounded without breaking that promise. Store
calls wait on locks and I/O, so they must not run on the event loop that serves
every other request, and a busy database is a retryable 503, not a 500.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omnidesk_agent.appsync.store import SYNC_PAGE_LIMIT
from omnidesk_agent.appsync.strict_json_store import StrictJsonAppSyncStore
from omnidesk_agent.config import AppConfig
from omnidesk_agent.server import create_app

OWNER = "olivia-owner"


def _pages(store: Any, since: int, *, limit: int, actor: str | None = None) -> tuple[list[int], int, int]:
    cursor, seqs, calls = since, [], 0
    while True:
        page = store.sync_since(cursor, actor=actor, limit=limit)
        calls += 1
        assert page["reset_required"] is False
        seqs.extend(event["seq"] for event in page["events"])
        cursor = page["sync_seq"]
        if not page["has_more"]:
            return seqs, cursor, calls


# ------------------------------------------------------------------ JSON ---


def test_json_feed_pages_oldest_first_and_resumes_without_gaps(tmp_path: Path) -> None:
    store = StrictJsonAppSyncStore(tmp_path / "appsync.json")
    for index in range(25):
        store.create_conversation(actor=OWNER, title=f"c{index}")
    head = store.sync_since(0)["head_seq"]
    seqs, cursor, calls = _pages(store, 0, limit=10)
    assert seqs == list(range(1, head + 1))
    assert cursor == head and calls == 3
    first = store.sync_since(0, limit=10)
    assert first["has_more"] is True and first["sync_seq"] == first["events"][-1]["seq"] == 10


def test_json_cursor_below_retained_history_must_rebootstrap(tmp_path: Path) -> None:
    """The in-memory store keeps the newest 1000 events; older cursors are reset."""
    store = StrictJsonAppSyncStore(tmp_path / "appsync.json")
    for index in range(1005):
        store.record_event("probe.event", OWNER, {"organization_id": "org_default", "n": index})
    stale = store.sync_since(0)
    assert stale["reset_required"] is True and stale["events"] == []
    assert stale["sync_seq"] == stale["head_seq"] == store._seq
    fresh = store.sync_since(stale["pruned_through"], limit=SYNC_PAGE_LIMIT)
    assert fresh["reset_required"] is False and fresh["events"][0]["seq"] == stale["pruned_through"] + 1
    # A cursor ahead of the head (history rewound, e.g. a restored store) also resets.
    assert store.sync_since(store._seq + 50)["reset_required"] is True


# ------------------------------------------------------------ PostgreSQL ---


def _dsn() -> str:
    value = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "").strip()
    if not value:
        pytest.skip("OMNIDESK_TEST_POSTGRES_DSN is not configured")
    return value


def _pg_store(prefix: str):
    from omnidesk_agent.appsync.migrated_postgres_store import MigratedMultiInstancePostgresAppSyncStore
    from omnidesk_agent.appsync.postgres_migrations import apply_appsync_migrations

    namespace = f"{prefix}_{uuid.uuid4().hex}"
    apply_appsync_migrations(_dsn(), namespace=namespace)
    return MigratedMultiInstancePostgresAppSyncStore(dsn=_dsn(), namespace=namespace, pool_size=3)


def _age_events(store: Any, *, through_seq: int, days: float) -> None:
    with store._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE omnidesk_appsync_sync_events SET created_at = created_at - %s WHERE namespace = %s AND seq <= %s",
            (days * 86_400, store.namespace, through_seq),
        )


def test_postgres_feed_pages_and_signals_rewound_history() -> None:
    store = _pg_store("paging")
    try:
        for index in range(12):
            store.create_conversation(actor=OWNER, title=f"c{index}")
        head = store.sync_since(0)["head_seq"]
        seqs, cursor, calls = _pages(store, 0, limit=5, actor=OWNER)
        assert seqs == list(range(1, head + 1)) and cursor == head and calls == 3
        assert store.sync_since(head + 10)["reset_required"] is True
    finally:
        store.close()


def test_retention_prunes_old_events_and_resets_readers_below_the_watermark() -> None:
    from omnidesk_agent.appsync.retention import run_retention

    store = _pg_store("event_retention")
    try:
        for index in range(30):
            store.create_conversation(actor=OWNER, title=f"c{index}")
        head = store.sync_since(0)["head_seq"]
        _age_events(store, through_seq=20, days=30)
        report = run_retention(store, sync_event_days=14)
        assert report["tables"]["sync_events"] == {"deleted": 20, "pruned_through": 20}
        assert store.sync_since(5)["reset_required"] is True
        after = store.sync_since(20)
        assert after["reset_required"] is False and [e["seq"] for e in after["events"]] == list(range(21, head + 1))
        # New events keep their numbers; the counter is not affected by pruning.
        assert store.record_event("probe.event", OWNER, {"organization_id": "org_default"})["seq"] == head + 1
        assert run_retention(store, sync_event_days=14)["tables"]["sync_events"]["deleted"] == 0
    finally:
        store.close()


def test_watermark_is_committed_before_any_event_is_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    """If deletion fails half way, readers below the cut are still told to reset."""
    from omnidesk_agent.appsync import retention

    store = _pg_store("watermark_first")
    try:
        for index in range(10):
            store.create_conversation(actor=OWNER, title=f"c{index}")
        _age_events(store, through_seq=6, days=30)

        def fail(*_args: Any, **_kwargs: Any) -> int:
            raise RuntimeError("deletion interrupted")

        monkeypatch.setattr(retention, "_delete_batches", fail)
        with pytest.raises(RuntimeError, match="deletion interrupted"):
            retention.run_retention(store, sync_event_days=14, rules=())
        assert store.sync_since(0)["reset_required"] is True
        assert store.sync_since(6)["reset_required"] is False
    finally:
        store.close()


def test_retention_rules_delete_only_expired_rows() -> None:
    from omnidesk_agent.appsync.retention import run_retention

    store = _pg_store("table_retention")
    now = time.time()
    old, young = now - 400 * 86_400, now - 60
    namespace = store.namespace
    try:
        with store._connect() as conn, conn.cursor() as cur:
            for key, created in (("old", old), ("young", young)):
                cur.execute(
                    "INSERT INTO omnidesk_appsync_idempotency_keys(namespace, organization_id, actor, endpoint, key, payload_hash, response, created_at) VALUES (%s,'org_default','a','e',%s,'h','{}'::jsonb,%s)",
                    (namespace, key, created),
                )
            for push_id, status, updated in (("push_old_sent", "sent", old), ("push_old_pending", "pending", old), ("push_young_sent", "sent", young)):
                cur.execute(
                    "INSERT INTO omnidesk_appsync_push_outbox(namespace, organization_id, push_id, device_id, platform, audience, title, body, status, created_at, updated_at) VALUES (%s,'org_default',%s,'d','ios','mobile','t','b',%s,%s,%s)",
                    (namespace, push_id, status, updated, updated),
                )
            for enrollment_id, status in (("enroll_old_completed", "completed"), ("enroll_old_expired", "expired")):
                cur.execute(
                    "INSERT INTO omnidesk_appsync_device_enrollments(namespace, organization_id, enrollment_id, device_type, requested_by, pairing_code_hash, status, created_at, expires_at) VALUES (%s,'org_default',%s,'mobile','o','h',%s,%s,%s)",
                    (namespace, enrollment_id, status, old, old + 600),
                )
            cur.execute("INSERT INTO omnidesk_appsync_device_request_nonces(namespace, nonce_key, expires_at) VALUES (%s,'dead',%s), (%s,'live',%s)", (namespace, now - 1, namespace, now + 600))
        dry = run_retention(store, dry_run=True)
        assert dry["tables"]["idempotency_keys"]["would_delete"] == 1
        with store._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM omnidesk_appsync_idempotency_keys WHERE namespace = %s", (namespace,))
            assert cur.fetchone() == (2,)
        report = run_retention(store)["tables"]
        assert report["idempotency_keys"]["deleted"] == 1
        assert report["push_outbox"]["deleted"] == 1
        assert report["device_enrollments"]["deleted"] == 1
        assert report["device_request_nonces"]["deleted"] == 1
        with store._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT key FROM omnidesk_appsync_idempotency_keys WHERE namespace = %s", (namespace,))
            assert cur.fetchall() == [("young",)]
            cur.execute("SELECT push_id FROM omnidesk_appsync_push_outbox WHERE namespace = %s ORDER BY push_id", (namespace,))
            # A pending push is never dropped by age; only delivered or failed ones are.
            assert cur.fetchall() == [("push_old_pending",), ("push_young_sent",)]
            cur.execute("SELECT enrollment_id FROM omnidesk_appsync_device_enrollments WHERE namespace = %s", (namespace,))
            # A completed enrollment is the record of which key a device was bound to.
            assert cur.fetchall() == [("enroll_old_completed",)]
            cur.execute("SELECT nonce_key FROM omnidesk_appsync_device_request_nonces WHERE namespace = %s", (namespace,))
            assert cur.fetchall() == [("live",)]
    finally:
        store.close()


def test_only_one_replica_runs_retention_at_a_time() -> None:
    import psycopg

    from omnidesk_agent.appsync.retention import _lock_key, run_retention

    store = _pg_store("retention_election")
    holder = psycopg.connect(_dsn(), autocommit=True)
    try:
        holder.execute("SELECT pg_advisory_lock(%s)", (_lock_key(store.namespace),))
        assert "skipped" in run_retention(store)
        holder.execute("SELECT pg_advisory_unlock(%s)", (_lock_key(store.namespace),))
        assert "skipped" not in run_retention(store)
    finally:
        holder.close()
        store.close()


def test_postgres_routes_start_background_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnidesk_agent import appsync

    started: list[float] = []

    class _Store:
        def start_retention(self, *, interval_seconds: float, metrics: Any = None, also: Any = None) -> None:
            started.append(interval_seconds)

    monkeypatch.setenv("OMNIDESK_APPSYNC_RETENTION_INTERVAL_SECONDS", "900")
    appsync._start_retention(_Store(), None)
    monkeypatch.setenv("OMNIDESK_APPSYNC_RETENTION_INTERVAL_SECONDS", "0")
    appsync._start_retention(_Store(), None)
    appsync._start_retention(object(), None)  # the JSON store has nothing to prune in the background
    assert started == [900.0]


# ---------------------------------------------------- event loop and 503 ---


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Any]:
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "owner-token")
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
    app = create_app(cfg)
    return TestClient(app), app.state.runtime.app_sync


def _auth() -> dict[str, str]:
    return {"authorization": "Bearer owner-token", "x-omnidesk-actor": OWNER}


def test_store_calls_run_off_the_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(tmp_path, monkeypatch)
    loop_threads: list[bool] = []
    original = store.bootstrap

    def recording_bootstrap(*args: Any, **kwargs: Any) -> Any:
        try:
            asyncio.get_running_loop()
            loop_threads.append(True)
        except RuntimeError:
            loop_threads.append(False)
        assert threading.current_thread().name.startswith("omnidesk-appsync-db")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "bootstrap", recording_bootstrap)
    response = client.get("/app/bootstrap", headers=_auth())
    assert response.status_code == 200
    assert loop_threads == [False], "store calls must not run on the event loop"


@pytest.mark.parametrize("busy", ["pool", "lock"])
def test_a_busy_database_is_a_retryable_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, busy: str) -> None:
    from omnidesk_agent.appsync.migrated_postgres_store import PoolTimeout

    client, store = _client(tmp_path, monkeypatch)
    if busy == "lock":
        errors = pytest.importorskip("psycopg.errors")
        error: Exception = errors.LockNotAvailable("canceling statement due to lock timeout")
    else:
        error = PoolTimeout("no PostgreSQL AppSync connection became free within 10s")

    def busy_bootstrap(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(store, "bootstrap", busy_bootstrap)
    response = client.get("/app/bootstrap", headers=_auth())
    assert response.status_code == 503
    assert response.headers["retry-after"] == "2"
    assert response.json()["detail"]["code"] == "database_busy"


def test_websocket_catch_up_sends_pages_back_to_back() -> None:
    from omnidesk_agent.appsync.sync_websocket import stream_sync_events

    pages = [
        {"sync_seq": 2, "events": [{"seq": 1}, {"seq": 2}], "has_more": True},
        {"sync_seq": 4, "events": [{"seq": 3}, {"seq": 4}], "has_more": True},
        {"sync_seq": 5, "events": [{"seq": 5}], "has_more": False},
    ]
    cursors: list[int] = []

    class _Store:
        def sync_since(self, since_seq: int, actor: str | None = None) -> dict[str, Any]:
            cursors.append(since_seq)
            return pages[min(len(cursors) - 1, len(pages) - 1)]

    class _Socket:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []
            self.gate = asyncio.Event()

        async def send_json(self, message: dict[str, Any]) -> None:
            self.sent.append(message)
            if len(self.sent) == 3:
                self.gate.set()

        async def receive(self) -> dict[str, Any]:
            await self.gate.wait()
            return {"type": "websocket.disconnect"}

    async def run() -> _Socket:
        socket = _Socket()
        started = time.monotonic()
        await asyncio.wait_for(
            stream_sync_events(socket, _Store(), actor=OWNER, since_seq=0, poll_seconds=30.0, poll_slots=asyncio.Semaphore(1)),
            timeout=5,
        )
        assert time.monotonic() - started < 5
        return socket

    socket = asyncio.run(run())
    assert cursors[:3] == [0, 2, 4]
    assert [event["seq"] for message in socket.sent for event in message["events"]] == [1, 2, 3, 4, 5]
