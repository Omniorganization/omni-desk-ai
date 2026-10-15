from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Barrier, Event, Lock

import pytest

from omnidesk_agent.repositories.postgres import PostgresRepositoryFactory
from omnidesk_agent.repositories.postgres_pool import PostgresUnavailable, SharedPostgresConnectionPool


class Connection:
    def __init__(self):
        self.closed = False
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _wait_for_waiter(pool):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if pool.stats()["waiters"]:
            return
        time.sleep(0.005)
    raise AssertionError("worker never entered acquisition wait")


def test_original_pool_close_during_connect_rejects_and_closes_late_socket(monkeypatch):
    entered, release = Event(), Event()
    connection = Connection()
    pool = SharedPostgresConnectionPool("postgresql://synthetic")

    def connect():
        entered.set()
        assert release.wait(2)
        return connection

    monkeypatch.setattr(pool, "_new_connection", connect)

    def worker():
        with pytest.raises(PostgresUnavailable, match="closed"):
            with pool.connection():
                pytest.fail("closed pool must not lend a late socket")

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker)
        try:
            assert entered.wait(2)
            pool.close()
        finally:
            release.set()
        future.result(timeout=2)
    assert connection.closed
    assert pool.stats()["created"] == pool.stats()["in_use"] == 0


def test_original_pool_close_wakes_waiters_before_acquisition_timeout(monkeypatch):
    pool = SharedPostgresConnectionPool("postgresql://synthetic", max_size=2, acquire_timeout_seconds=5)
    monkeypatch.setattr(pool, "_new_connection", Connection)

    def waiter():
        with pytest.raises(PostgresUnavailable, match="closed"):
            with pool.connection():
                pytest.fail("closed pool must reject waiting acquisitions")

    with ExitStack() as stack, ThreadPoolExecutor(max_workers=1) as executor:
        first = stack.enter_context(pool.connection())
        second = stack.enter_context(pool.connection())
        future = executor.submit(waiter)
        try:
            _wait_for_waiter(pool)
            pool.close()
            future.result(timeout=1)
            assert not first.closed and not second.closed
        finally:
            stack.close()
    assert first.closed and second.closed
    assert pool.stats()["created"] == pool.stats()["waiters"] == 0


def test_failed_connect_releases_capacity_and_wakes_another_waiter(monkeypatch):
    pool = SharedPostgresConnectionPool("postgresql://synthetic", max_size=2, acquire_timeout_seconds=5)
    failed_entered, fail = Event(), Event()
    call_lock = Lock()
    calls = 0

    def connect():
        nonlocal calls
        with call_lock:
            calls += 1
            current = calls
        if current == 2:
            failed_entered.set()
            assert fail.wait(2)
            raise RuntimeError("connection refused")
        return Connection()

    monkeypatch.setattr(pool, "_new_connection", connect)

    def worker():
        with pool.connection():
            return True

    with pool.connection(), ThreadPoolExecutor(max_workers=2) as executor:
        failure = executor.submit(worker)
        try:
            assert failed_entered.wait(2)
            successor = executor.submit(worker)
            _wait_for_waiter(pool)
        finally:
            fail.set()
        with pytest.raises(RuntimeError, match="refused"):
            failure.result(timeout=1)
        assert successor.result(timeout=1)
    assert calls == 3
    assert pool.stats()["waiters"] == pool.stats()["in_use"] == 0
    pool.close()
    assert pool.stats()["created"] == 0


def test_connect_cancellation_does_not_leak_reserved_capacity(monkeypatch):
    class Cancelled(BaseException):
        pass

    pool = SharedPostgresConnectionPool("postgresql://synthetic")

    def connect():
        raise Cancelled()

    monkeypatch.setattr(pool, "_new_connection", connect)
    with pytest.raises(Cancelled):
        with pool.connection():
            pytest.fail("cancelled connection must not be yielded")
    assert pool.stats()["created"] == 0
    monkeypatch.setattr(pool, "_new_connection", Connection)
    with pool.connection():
        pass
    pool.close()


@pytest.mark.parametrize("timeout", [float("inf"), float("nan"), 0, -1])
@pytest.mark.parametrize("field", ["acquire_timeout_seconds", "connect_timeout_seconds"])
def test_pool_rejects_unbounded_or_nonpositive_timeouts(field, timeout):
    with pytest.raises(PostgresUnavailable, match="finite and positive"):
        SharedPostgresConnectionPool("postgresql://synthetic", **{field: timeout})


def test_saturated_pool_timeout_is_bounded_and_leaves_accounting_intact(monkeypatch):
    pool = SharedPostgresConnectionPool("postgresql://synthetic", max_size=2, acquire_timeout_seconds=0.1)
    monkeypatch.setattr(pool, "_new_connection", Connection)
    with pool.connection(), pool.connection():
        with pytest.raises(PostgresUnavailable, match="timed out"):
            with pool.connection():
                pytest.fail("capacity must remain bounded")
        assert pool.stats()["created"] == pool.stats()["in_use"] == 2
        assert pool.stats()["waiters"] == 0
    pool.close()


def test_failed_rollback_discards_connection_and_preserves_original_error(monkeypatch):
    class BrokenRollback(Connection):
        def rollback(self):
            raise RuntimeError("rollback unavailable")

    connection = BrokenRollback()
    pool = SharedPostgresConnectionPool("postgresql://synthetic")
    monkeypatch.setattr(pool, "_new_connection", lambda: connection)
    with pytest.raises(ValueError, match="transaction failure"):
        with pool.connection():
            raise ValueError("transaction failure")
    assert connection.closed
    assert pool.stats()["created"] == pool.stats()["in_use"] == 0
    pool.close()


def test_cleanup_failure_cannot_mask_transaction_failure_or_stop_drain(monkeypatch, caplog):
    class BrokenCleanup(Connection):
        def rollback(self):
            raise RuntimeError("rollback unavailable")

        def close(self):
            raise RuntimeError("sensitive connection details")

    pool = SharedPostgresConnectionPool("postgresql://synthetic")
    monkeypatch.setattr(pool, "_new_connection", BrokenCleanup)
    with pytest.raises(ValueError, match="original"):
        with pool.connection():
            raise ValueError("original")
    assert "sensitive connection details" not in caplog.text
    assert pool.stats()["created"] == 0
    monkeypatch.setattr(pool, "_new_connection", Connection)
    with pool.connection() as connection:
        pass
    pool.close()
    pool.close()
    assert connection.closed


def test_factory_initializes_one_shared_pool_under_concurrency(monkeypatch):
    import omnidesk_agent.repositories.postgres as postgres

    factory = PostgresRepositoryFactory("postgresql://synthetic")
    barrier = Barrier(8)
    constructor_lock = Lock()
    creations = []

    def create_pool(*args, **kwargs):
        with constructor_lock:
            creations.append(True)
        time.sleep(0.05)
        return SharedPostgresConnectionPool(*args, **kwargs)

    monkeypatch.setattr(postgres, "SharedPostgresConnectionPool", create_pool)

    def worker():
        barrier.wait(timeout=2)
        return factory._connection_pool()

    with ThreadPoolExecutor(max_workers=8) as executor:
        pools = list(executor.map(lambda _: worker(), range(8)))
    assert all(pool is pools[0] for pool in pools)
    assert len(creations) == 1
    factory.close()
    assert pools[0].stats()["closed"]


def test_factory_close_before_initialization_prevents_resurrection():
    factory = PostgresRepositoryFactory("postgresql://synthetic")
    factory.close()
    factory.close()
    for getter in (factory.readiness_check, factory.transactional_outbox, factory.job_queue):
        with pytest.raises(PostgresUnavailable, match="closed"):
            getter()
    assert factory._pool is None
