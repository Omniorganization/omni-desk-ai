from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from omnidesk_agent.repositories.postgres import (
    PostgresRepositoryFactory,
    PostgresTransactionalOutboxRepository,
)
from omnidesk_agent.repositories.postgres_pool import (
    PostgresUnavailable,
    SharedPostgresConnectionPool,
)
from omnidesk_agent.repositories.postgres_state import PostgresJobQueue, PostgresRunStore


class FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        self.executed.append((str(query), params))

    def fetchone(self):
        return (1,)


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False
        self.commits = 0
        self.rollbacks = 0
        self.cursor_instance = FakeCursor()

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def test_shared_postgres_pool_reuses_connections_and_exposes_pressure(
    monkeypatch,
) -> None:
    opened: list[FakeConnection] = []

    def connect(*args, **kwargs):
        connection = FakeConnection()
        opened.append(connection)
        return connection

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    pool = SharedPostgresConnectionPool("postgresql://example", max_size=2)
    with pool.connection() as first:
        assert isinstance(first, FakeConnection)
    with pool.connection() as second:
        assert second is first
    stats = pool.stats()
    assert stats == {
        "max_size": 2,
        "created": 1,
        "in_use": 0,
        "idle": 1,
        "waiters": 0,
        "closed": False,
    }
    assert pool.ping()["ok"] is True
    assert len(opened) == 1
    pool.close()
    assert opened[0].closed is True


def test_shared_postgres_pool_rolls_back_failed_units(monkeypatch) -> None:
    connection = FakeConnection()
    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=lambda *a, **k: connection),
    )
    pool = SharedPostgresConnectionPool("postgresql://example", max_size=2)
    with pytest.raises(RuntimeError):
        with pool.connection():
            raise RuntimeError("boom")
    assert connection.rollbacks == 1
    assert pool.stats()["idle"] == 1


def test_outbox_health_check_validates_required_schema_after_startup(
    monkeypatch,
) -> None:
    connection = FakeConnection()
    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=lambda *a, **k: connection),
    )
    pool = SharedPostgresConnectionPool("postgresql://example", max_size=2)
    outbox = PostgresTransactionalOutboxRepository(pool)
    outbox._schema_ready = True

    assert outbox.health_check() == {"ok": True}
    query = connection.cursor_instance.executed[-1][0]
    assert "FROM transactional_outbox" in query
    for column in (
        "id",
        "dedupe_key",
        "topic",
        "payload_json",
        "status",
        "retry_count",
        "locked_at",
        "created_at",
        "updated_at",
        "last_error",
    ):
        assert column in query


def test_factory_deep_health_includes_outbox_and_pool_status() -> None:
    factory = PostgresRepositoryFactory("postgresql://example")
    outbox = SimpleNamespace(health_check=lambda: {"ok": True})
    runtime = SimpleNamespace(health_check=lambda: {"ok": True, "state": "ready"})
    pool = SimpleNamespace(
        stats=lambda: {
            "max_size": 4,
            "created": 1,
            "in_use": 0,
            "idle": 1,
            "waiters": 0,
            "closed": False,
        }
    )
    object.__setattr__(factory, "_outbox", outbox)
    object.__setattr__(factory, "_runtime", runtime)
    object.__setattr__(factory, "_pool", pool)

    report = factory.health_check()

    assert report["ok"] is True
    assert report["transactional_outbox"] == {"ok": True}
    assert report["pool"]["max_size"] == 4


class SqlAwareState:
    def __init__(self) -> None:
        self.claim_calls: list[dict[str, object]] = []

    def claim_ready_by_status(self, namespace, **kwargs):
        self.claim_calls.append({"namespace": namespace, **kwargs})
        return {"id": "job-1", "status": "pending"}

    def claim_one(self, *args, **kwargs):
        raise AssertionError("generic Python-side claim path must not be used")

    def find_by_field(self, namespace, field, value):
        return {"namespace": namespace, field: value}


def test_job_claim_and_run_lookup_use_sql_optimized_state_paths() -> None:
    state = SqlAwareState()
    claimed = PostgresJobQueue(state).claim_next()
    assert claimed == {"id": "job-1", "status": "pending"}
    assert state.claim_calls[0]["statuses"] == ("pending", "retry")
    run = PostgresRunStore(state).get_by_approval("approval-1")
    assert run == {"namespace": "runs", "waiting_approval_id": "approval-1"}


def test_pool_rejects_empty_dsn() -> None:
    with pytest.raises(PostgresUnavailable):
        SharedPostgresConnectionPool("")


def test_postgres_state_optimized_methods_are_class_owned() -> None:
    from omnidesk_agent.repositories.postgres_state import (
        PostgresRuntimeStateStores,
        _PostgresJsonState,
    )

    assert callable(getattr(_PostgresJsonState, "claim_ready_by_status", None))
    assert callable(getattr(_PostgresJsonState, "claim_one", None))
    assert callable(getattr(PostgresRuntimeStateStores, "readiness_check", None))
    assert callable(getattr(PostgresRuntimeStateStores, "close", None))
