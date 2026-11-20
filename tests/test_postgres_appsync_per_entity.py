"""Multi-instance correctness of the per-entity PostgreSQL AppSync store.

Replicas share nothing but the database. These tests run several store
instances (each with its own pool, as separate gateway pods would) against one
namespace and check the properties the previous whole-state emulation bought
with a namespace-wide lock: one winner per task claim and per approval decision,
idempotent writes, cross-instance visibility, and a sync feed whose sequence
numbers have no holes. They also check what the lock cost: per-call work must
not grow with the amount of stored data, and a stuck lock must fail a call
quickly instead of hanging it.

They run wherever OMNIDESK_TEST_POSTGRES_DSN is set (the CI coverage job).
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from omnidesk_agent.appsync.strict_json_store import StrictJsonAppSyncStore

OWNER = "olivia-owner"
APPROVER = "andrea-approver"  # approvals need someone other than the requester
OPERATOR = "oscar-operator"


def _dsn() -> str:
    value = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "").strip()
    if not value:
        pytest.skip("OMNIDESK_TEST_POSTGRES_DSN is not configured")
    return value


def _namespace(prefix: str) -> str:
    from omnidesk_agent.appsync.postgres_migrations import apply_appsync_migrations

    namespace = f"{prefix}_{uuid.uuid4().hex}"
    apply_appsync_migrations(_dsn(), namespace=namespace)
    return namespace


def _store(namespace: str, **kwargs: Any):
    from omnidesk_agent.appsync.migrated_postgres_store import MigratedMultiInstancePostgresAppSyncStore

    return MigratedMultiInstancePostgresAppSyncStore(dsn=_dsn(), namespace=namespace, pool_size=kwargs.pop("pool_size", 4), **kwargs)


@contextmanager
def _replicas(prefix: str, count: int = 2, **kwargs: Any) -> Iterator[list[Any]]:
    namespace = _namespace(prefix)
    stores = [_store(namespace, **kwargs) for _ in range(count)]
    try:
        yield stores
    finally:
        for store in stores:
            store.close()


def _hmac_sign(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def _verified_device(store: Any, *, actor: str, device_id: str, device_type: str, secret: str) -> None:
    """Pair and challenge-verify a device (legacy HMAC keys are allowed outside production)."""
    public_key = f"legacy-hmac:{secret}"
    store.register_device(actor=actor, device_id=device_id, device_type=device_type, name=device_id, platform="test", public_key=public_key)
    enrollment = store.start_device_enrollment(actor=OWNER, device_type=device_type, pairing_code="pairing-code-123")
    store.complete_device_enrollment(actor=actor, enrollment_id=enrollment["enrollment_id"], pairing_code="pairing-code-123", device_id=device_id, public_key=public_key)
    challenge = store.issue_device_challenge(actor=actor, enrollment_id=enrollment["enrollment_id"], device_id=device_id)
    store.verify_device_challenge(actor=actor, enrollment_id=enrollment["enrollment_id"], challenge_id=challenge["challenge_id"], device_id=device_id, signature=_hmac_sign(secret, challenge["signing_message"]))


def _signed(store: Any, *, device_id: str, secret: str, actor: str, nonce: str | None = None) -> tuple[bool, str]:
    timestamp = str(time.time())
    nonce = nonce or uuid.uuid4().hex
    body_hash = hashlib.sha256(b"{}").hexdigest()
    message = f"omnidesk-device-request:v1:POST:/app/x:{body_hash}:{timestamp}:{nonce}"
    return store.verify_device_request_signature(
        device_id=device_id, method="POST", path="/app/x", body=b"{}", timestamp=timestamp, nonce=nonce,
        signature=_hmac_sign(secret, message), expected_actor=actor,
    )


def _approved_desktop_tasks(store: Any, count: int) -> list[str]:
    conversation = store.create_conversation(actor=OWNER, title="desktop work")
    task_ids = []
    for index in range(count):
        created = store.add_message_and_task(actor=OWNER, conversation_id=conversation["conversation_id"], content=f"run job {index}", requires_desktop_runtime=True)
        store.decide_approval(approval_id=created["approval"]["approval_id"], actor=APPROVER, decision="approved")
        task_ids.append(created["task"]["task_id"])
    return task_ids


class _StatementCounter:
    """Counts the statements a store sends, by wrapping its pool's connections."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.statements: list[str] = []
        pool = store._connection_pool
        original = pool.connection
        counter = self

        @contextmanager
        def counted() -> Iterator[Any]:
            with original() as connection:
                yield _CountingConnection(connection, counter.statements)

        pool.connection = counted

    def measure(self, call: Any) -> int:
        self.statements.clear()
        call()
        return len(self.statements)


class _CountingCursor:
    def __init__(self, cursor: Any, statements: list[str]) -> None:
        self._cursor, self._statements = cursor, statements

    def __enter__(self) -> "_CountingCursor":
        self._cursor.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._cursor.__exit__(*exc)

    def execute(self, sql: Any, params: Any = None) -> Any:
        self._statements.append(str(sql))
        return self._cursor.execute(sql, params)

    def executemany(self, sql: Any, params: Any) -> Any:
        self._statements.append(str(sql))
        return self._cursor.executemany(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _CountingConnection:
    def __init__(self, connection: Any, statements: list[str]) -> None:
        self._connection, self._statements = connection, statements

    def cursor(self, *args: Any, **kwargs: Any) -> _CountingCursor:
        return _CountingCursor(self._connection.cursor(*args, **kwargs), self._statements)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


# --------------------------------------------------------------- contract ---


def test_postgres_store_implements_the_whole_public_contract() -> None:
    """Routes call one API on either backend; a missing method would be a 500 in production only."""
    from omnidesk_agent.appsync.migrated_postgres_store import MigratedMultiInstancePostgresAppSyncStore

    # create_approval_locked needs the caller to hold the in-memory lock; on
    # PostgreSQL the approval is created inside add_message_and_task's transaction.
    internal = {"create_approval_locked"}
    public = {name for name, value in inspect.getmembers(StrictJsonAppSyncStore) if callable(value) and not name.startswith("_")}
    missing = sorted(name for name in public - internal if not callable(getattr(MigratedMultiInstancePostgresAppSyncStore, name, None)))
    assert missing == []


def _scenario(store: Any) -> list[Any]:
    """One cross-device flow through public methods, reduced to comparable facts."""
    trace: list[Any] = []
    store.ensure_user(OWNER)
    store.ensure_user(OPERATOR)
    _verified_device(store, actor=OWNER, device_id="dev_phone", device_type="mobile", secret="phone-secret")
    store.register_push_token(actor=OWNER, device_id="dev_phone", push_token="phone-push-token")
    store.register_device(actor=OWNER, device_id="dev_desk", device_type="desktop", name="desk", platform="mac")
    conversation = store.create_conversation(actor=OWNER, title="ops", idempotency_key="conv-1", idempotency_payload={"title": "ops"})
    again = store.create_conversation(actor=OWNER, title="ops", idempotency_key="conv-1", idempotency_payload={"title": "ops"})
    trace.append(("conversation replay", again == conversation))
    with pytest.raises(Exception) as conflict:
        store.create_conversation(actor=OWNER, title="other", idempotency_key="conv-1", idempotency_payload={"title": "other"})
    trace.append(("conversation conflict", type(conflict.value).__name__))
    created = store.add_message_and_task(actor=OWNER, conversation_id=conversation["conversation_id"], content="deploy", requires_desktop_runtime=True)
    trace.append(("task", created["task"]["status"], created["approval"]["status"], created["approval"]["risk"]))
    trace.append(("pushes", sorted(item["device_id"] for item in store.pending_push_outbox())))
    approval_id = created["approval"]["approval_id"]
    trace.append(("decide", store.decide_approval(approval_id=approval_id, actor=APPROVER, decision="approved")["status"]))
    with pytest.raises(ValueError) as decided:
        store.decide_approval(approval_id=approval_id, actor=APPROVER, decision="rejected")
    trace.append(("decide twice", str(decided.value)))
    claimed = store.claim_next_task(actor=OWNER, device_id="dev_desk", capabilities=["dry_run"])
    trace.append(("claim", claimed["status"], claimed["attempt_count"], store.claim_next_task(actor=OWNER, device_id="dev_desk")))
    trace.append(("approval after claim", [a["status"] for a in store.list_approvals(actor=OWNER)]))
    with pytest.raises(PermissionError) as rogue:
        store.update_task_status(task_id=claimed["task_id"], actor=OWNER, status="completed", device_id="dev_phone")
    trace.append(("rogue report", str(rogue.value)))
    done = store.update_task_status(task_id=claimed["task_id"], actor=OWNER, status="completed", result_summary="ok", device_id="dev_desk")
    trace.append(("done", done["status"], done["result_summary"], done["lease_expires_at"]))
    trace.append(("operator sees task", "conversation_id" in store.get_task(claimed["task_id"], actor=OPERATOR)))
    first = _signed(store, device_id="dev_phone", secret="phone-secret", actor=OWNER, nonce="n" * 20)
    replay = _signed(store, device_id="dev_phone", secret="phone-secret", actor=OWNER, nonce="n" * 20)
    trace.append(("signatures", first, replay, _signed(store, device_id="dev_phone", secret="phone-secret", actor=OPERATOR)))
    for _ in range(3):
        store.heartbeat_runtime(actor=OWNER, device_id="dev_desk", status="online", version="1")
    store.heartbeat_runtime(actor=OWNER, device_id="dev_desk", status="degraded", version="1")
    snapshot = store.bootstrap(OWNER, role="owner")
    trace.append(("bootstrap", sorted(d["device_id"] for d in snapshot["devices"]), [r["status"] for r in snapshot["runtime_status"]], len(snapshot["pending_approvals"]), "push_token" in json.dumps(snapshot["devices"])))
    trace.append(("viewer devices", sorted(d["device_id"] for d in store.bootstrap(OPERATOR, role="viewer")["devices"])))
    trace.append(("notifications", sorted(n["event_type"] for n in store.list_notifications(actor=OWNER))))
    store.revoke_device(actor=OWNER, device_id="dev_phone", reason="lost")
    trace.append(("after revoke", _signed(store, device_id="dev_phone", secret="phone-secret", actor=OWNER), store.pending_push_outbox()))
    feed = store.sync_since(0, actor=OWNER)
    trace.append(("feed", [event["event_type"] for event in feed["events"]]))
    trace.append(("feed head", feed["sync_seq"] == max(event["seq"] for event in feed["events"])))
    return trace


def test_postgres_and_json_backends_behave_the_same(tmp_path: Path) -> None:
    """The JSON store is the development double of the PostgreSQL store: the same
    calls must yield the same states, errors, visibility and event stream."""
    namespace = _namespace("parity")
    postgres = _store(namespace)
    try:
        expected = _scenario(StrictJsonAppSyncStore(tmp_path / "appsync.json"))
        assert _scenario(postgres) == expected
    finally:
        postgres.close()


def test_heartbeats_and_signed_requests_do_not_grow_the_sync_feed(tmp_path: Path) -> None:
    """Liveness is not an event: a desktop beats every few seconds, forever."""
    for store_factory in (lambda: StrictJsonAppSyncStore(tmp_path / "liveness.json"), lambda: _store(_namespace("liveness"))):
        store = store_factory()
        try:
            store.ensure_user(OWNER)
            _verified_device(store, actor=OWNER, device_id="dev_desk", device_type="desktop", secret="s")
            store.heartbeat_runtime(actor=OWNER, device_id="dev_desk", status="online")
            head = store.sync_since(0)["sync_seq"]
            for _ in range(20):
                store.heartbeat_runtime(actor=OWNER, device_id="dev_desk", status="online")
                assert _signed(store, device_id="dev_desk", secret="s", actor=OWNER) == (True, "ok")
            assert store.sync_since(0)["sync_seq"] == head
            store.heartbeat_runtime(actor=OWNER, device_id="dev_desk", status="degraded")
            changed = store.sync_since(head)["events"]
            assert [event["event_type"] for event in changed] == ["runtime.status_changed"]
            assert [r["status"] for r in store.bootstrap(OWNER, role="owner")["runtime_status"]] == ["degraded"]
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()


# ------------------------------------------------------------ concurrency ---


def test_every_task_is_claimed_exactly_once_across_replicas() -> None:
    with _replicas("claims", count=3, pool_size=6) as stores:
        stores[0].ensure_user(OWNER)
        for index in range(3):
            stores[0].register_device(actor=OWNER, device_id=f"dev_desk_{index}", device_type="desktop", name="desk", platform="mac")
        task_ids = _approved_desktop_tasks(stores[0], 24)
        claims: list[tuple[str, str]] = []
        lock = threading.Lock()
        start = threading.Barrier(9)

        def worker(store: Any, device_id: str) -> None:
            start.wait(timeout=30)
            while True:
                task = store.claim_next_task(actor=OWNER, device_id=device_id)
                if task is None:
                    return
                with lock:
                    claims.append((task["task_id"], device_id))

        with ThreadPoolExecutor(max_workers=9) as pool:
            futures = [pool.submit(worker, stores[i % 3], f"dev_desk_{i % 3}") for i in range(9)]
            for future in futures:
                future.result(timeout=120)
        claimed = [task_id for task_id, _ in claims]
        assert sorted(claimed) == sorted(task_ids)
        assert len(set(claimed)) == len(claimed)
        statuses = {a["task_id"]: a["status"] for a in stores[1].list_approvals(actor=OWNER)}
        assert set(statuses.values()) == {"consumed"} and set(statuses) == set(task_ids)


def test_one_decision_wins_and_idempotent_writes_run_once_across_replicas() -> None:
    with _replicas("decisions", count=2, pool_size=6) as stores:
        stores[0].ensure_user(OWNER)
        conversation = stores[0].create_conversation(actor=OWNER, title="ops")
        created = stores[0].add_message_and_task(actor=OWNER, conversation_id=conversation["conversation_id"], content="risky", risk="high")
        approval_id = created["approval"]["approval_id"]
        start = threading.Barrier(6)

        def decide(index: int) -> Any:
            start.wait(timeout=30)
            try:
                return stores[index % 2].decide_approval(approval_id=approval_id, actor=APPROVER, decision="approved" if index % 2 else "rejected")["status"]
            except ValueError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(decide, range(6)))
        assert sorted(outcomes).count("approval already decided") == 5
        winner = next(item for item in outcomes if item != "approval already decided")
        task = stores[1].get_task(created["task"]["task_id"], actor=OWNER)
        assert task["status"] == ("queued" if winner == "approved" else "cancelled")

        payload = {"content": "same request"}
        replay_start = threading.Barrier(6)

        def create(index: int) -> Any:
            replay_start.wait(timeout=30)
            return stores[index % 2].add_message_and_task(actor=OWNER, conversation_id=conversation["conversation_id"], content="same request", idempotency_key="idem-1", idempotency_payload=payload)

        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(create, range(6)))
        assert all(response == responses[0] for response in responses)
        with stores[0]._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM omnidesk_appsync_tasks WHERE namespace = %s AND title = %s", (stores[0].namespace, "same request"))
            assert cur.fetchone() == (1,)


def test_revocation_and_nonces_are_visible_to_every_replica_at_once() -> None:
    with _replicas("revocation", count=2) as (first, second):
        first.ensure_user(OWNER)
        _verified_device(first, actor=OWNER, device_id="dev_phone", device_type="mobile", secret="k")
        assert _signed(second, device_id="dev_phone", secret="k", actor=OWNER, nonce="x" * 20) == (True, "ok")
        assert _signed(first, device_id="dev_phone", secret="k", actor=OWNER, nonce="x" * 20) == (False, "nonce_replay")
        first.revoke_device(actor=OWNER, device_id="dev_phone", reason="lost")
        assert _signed(second, device_id="dev_phone", secret="k", actor=OWNER) == (False, "device_revoked")
        with pytest.raises(PermissionError, match="revoked"):
            second.register_push_token(actor=OWNER, device_id="dev_phone", push_token="new-token")


def test_sync_sequence_is_gapless_in_commit_order_under_concurrent_writers() -> None:
    """A reader that sees event N has seen every event below N.

    That is what lets clients read the feed incrementally from a cursor; a
    sequence that commits out of order would make a reader skip events forever.
    """
    with _replicas("gapless", count=3, pool_size=6) as stores:
        stores[0].ensure_user(OWNER)
        stop = threading.Event()
        problems: list[str] = []
        observed: list[int] = []

        def reader() -> None:
            cursor = stores[2].sync_since(0)["sync_seq"]
            while not stop.is_set() or cursor < stores[2].sync_since(0)["sync_seq"]:
                page = stores[2].sync_since(cursor)
                seqs = [event["seq"] for event in page["events"]]
                if seqs and seqs != list(range(cursor + 1, cursor + 1 + len(seqs))):
                    problems.append(f"after {cursor} read {seqs[:5]}")
                if seqs and page["sync_seq"] != seqs[-1]:
                    problems.append(f"head {page['sync_seq']} beyond page {seqs[-1]}")
                observed.extend(seqs)
                cursor = page["sync_seq"]

        def writer(index: int) -> None:
            store = stores[index % 2]
            for item in range(15):
                store.create_conversation(actor=OWNER, title=f"c{index}-{item}")

        reading = threading.Thread(target=reader)
        reading.start()
        with ThreadPoolExecutor(max_workers=6) as pool:
            for future in [pool.submit(writer, index) for index in range(6)]:
                future.result(timeout=120)
        stop.set()
        reading.join(timeout=60)
        assert problems == []
        head = stores[0].sync_since(0)["sync_seq"]
        everything = [event["seq"] for event in stores[1].sync_since(0)["events"]]
        assert everything == list(range(1, head + 1))
        assert observed and observed == sorted(set(observed))


# ------------------------------------------------------- cost and timeouts ---


def test_per_call_statements_do_not_grow_with_stored_data() -> None:
    """The emulation reloaded every table and re-upserted every row per call."""
    namespace = _namespace("cost")
    store = _store(namespace)
    try:
        store.ensure_user(OWNER)
        store.register_device(actor=OWNER, device_id="dev_desk", device_type="desktop", name="desk", platform="mac")
        conversation = store.create_conversation(actor=OWNER, title="ops")
        # The first heartbeat is a status change and emits an event; later ones do not.
        store.heartbeat_runtime(actor=OWNER, device_id="dev_desk", status="online")
        counter = _StatementCounter(store)

        def calls() -> dict[str, int]:
            return {
                "heartbeat": counter.measure(lambda: store.heartbeat_runtime(actor=OWNER, device_id="dev_desk", status="online")),
                "claim": counter.measure(lambda: store.claim_next_task(actor=OWNER, device_id="dev_desk")),
                "bootstrap": counter.measure(lambda: store.bootstrap(OWNER, role="owner")),
                "messages": counter.measure(lambda: store.list_messages(conversation["conversation_id"], actor=OWNER)),
                "conversation": counter.measure(lambda: store.create_conversation(actor=OWNER, title="probe")),
            }

        small = calls()
        for index in range(150):
            store.add_message_and_task(actor=OWNER, conversation_id=conversation["conversation_id"], content=f"m{index}")
        large = calls()
        assert large == small
        assert max(small.values()) <= 12
    finally:
        store.close()


def test_connections_carry_statement_and_lock_timeouts() -> None:
    from omnidesk_agent.appsync.migrated_postgres_store import PostgresTimeouts

    namespace = _namespace("timeouts")
    store = _store(namespace, timeouts=PostgresTimeouts(statement_timeout_ms=4321, lock_timeout_ms=300, idle_in_transaction_timeout_ms=9876))
    try:
        with store._connect() as conn, conn.cursor() as cur:
            cur.execute("SHOW statement_timeout")
            statement = cur.fetchone()[0]
            cur.execute("SHOW lock_timeout")
            lock = cur.fetchone()[0]
            cur.execute("SHOW idle_in_transaction_session_timeout")
            idle = cur.fetchone()[0]
        assert (statement, lock, idle) == ("4321ms", "300ms", "9876ms")
        assert store.health_details()["lock_timeout_ms"] == 300
    finally:
        store.close()


def test_a_stuck_row_lock_fails_the_call_instead_of_hanging_it() -> None:
    import psycopg
    from psycopg import errors

    from omnidesk_agent.appsync.migrated_postgres_store import PostgresTimeouts

    namespace = _namespace("stuck_lock")
    store = _store(namespace, timeouts=PostgresTimeouts(lock_timeout_ms=200))
    holder = psycopg.connect(_dsn())
    try:
        store.ensure_user(OWNER)
        store.register_device(actor=OWNER, device_id="dev_phone", device_type="mobile", name="p", platform="ios")
        with holder.cursor() as cur:
            cur.execute("SELECT 1 FROM omnidesk_appsync_devices WHERE namespace = %s AND device_id = %s FOR UPDATE", (namespace, "dev_phone"))
        started = time.monotonic()
        with pytest.raises(errors.LockNotAvailable):
            store.register_push_token(actor=OWNER, device_id="dev_phone", push_token="token")
        assert time.monotonic() - started < 5
        # Other rows, and reads of the locked one, are unaffected.
        assert store.ensure_user(OPERATOR)["user_id"] == OPERATOR
        assert any(d["device_id"] == "dev_phone" for d in store.bootstrap(OWNER, role="owner")["devices"])
        holder.rollback()
        assert store.register_push_token(actor=OWNER, device_id="dev_phone", push_token="token")["push_token"] == "token"
    finally:
        holder.close()
        store.close()


def test_failed_calls_keep_their_pooled_connection() -> None:
    namespace = _namespace("pool_reuse")
    store = _store(namespace, pool_size=2)
    try:
        store.ensure_user(OWNER)
        for _ in range(5):
            with pytest.raises(KeyError):
                store.get_task("task_missing", actor=OWNER)
        assert store._connection_pool._created == 1
    finally:
        store.close()


def test_writes_wait_for_a_previous_release_replica_but_reads_do_not() -> None:
    """During a rolling upgrade a previous-release replica still takes the
    namespace lock exclusively and writes back its whole cache. Writes must not
    interleave with that write-back; reads need not wait."""
    import psycopg

    from omnidesk_agent.appsync.migrated_postgres_store import _lock_key_legacy

    namespace = _namespace("rolling")
    store = _store(namespace)
    legacy = psycopg.connect(_dsn(), autocommit=True)
    try:
        store.ensure_user(OWNER)
        legacy.execute("SELECT pg_advisory_lock(%s)", (_lock_key_legacy(namespace),))
        assert store.list_conversations(OWNER) == []
        finished = threading.Event()

        def write() -> None:
            store.create_conversation(actor=OWNER, title="after upgrade")
            finished.set()

        writer = threading.Thread(target=write)
        writer.start()
        assert not finished.wait(timeout=0.5)
        legacy.execute("SELECT pg_advisory_unlock(%s)", (_lock_key_legacy(namespace),))
        writer.join(timeout=10)
        assert finished.is_set()
        assert [c["title"] for c in store.list_conversations(OWNER)] == ["after upgrade"]
    finally:
        legacy.close()
        store.close()


def test_new_event_counter_continues_above_existing_events() -> None:
    """Migration 5 seeds the counter from existing events, and the counter never
    hands out a number an older replica already used."""
    namespace = _namespace("counter")
    store = _store(namespace)
    try:
        store.ensure_user(OWNER)
        with store._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(seq), 0) FROM omnidesk_appsync_sync_events WHERE namespace = %s", (namespace,))
            head = int(cur.fetchone()[0])
            # An event numbered by a previous-release replica, past the counter.
            cur.execute(
                "INSERT INTO omnidesk_appsync_sync_events(namespace, organization_id, seq, event_type, actor, payload, created_at) VALUES (%s, 'org_default', %s, 'legacy.event', 'system', '{}'::jsonb, 0)",
                (namespace, head + 5),
            )
        event = store.record_event("probe.event", OWNER, {"organization_id": "org_default"})
        assert event["seq"] == head + 6
    finally:
        store.close()

