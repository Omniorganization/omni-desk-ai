"""The PostgreSQL rate limiter against a real server.

Runs where OMNIDESK_TEST_POSTGRES_DSN is set (CI's "Full Coverage + PostgreSQL
Atomicity" job). The 2026-11-21 audit (data-reliability, high) found the
limiter opening a connection per check, with no timeouts, and never pruning
its table. These tests show, on a real server, that the number of connections
stays the same under concurrent checks, expired windows are deleted, and a
locked or slow store fails a check within its timeout instead of hanging the
request.
"""

from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from omnidesk_agent.security.resource_guard import PostgresRateLimiter

DSN = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="OMNIDESK_TEST_POSTGRES_DSN is not configured")


@pytest.fixture()
def real(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    monkeypatch.setenv("OMNIDESK_TEST_RATE_LIMIT_DSN", DSN)
    started = time.time()
    clock = [started]
    limiter = PostgresRateLimiter(dsn_env="OMNIDESK_TEST_RATE_LIMIT_DSN", clock=lambda: clock[0], pool_size=3)
    yield SimpleNamespace(psycopg=psycopg, limiter=limiter, clock=clock, started=started, prefix=f"test-{uuid.uuid4().hex[:10]}")
    limiter.close()


def _query(psycopg, sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(DSN, autocommit=True) as con:
        return con.execute(sql, params).fetchall()


def _limiter_backends(real) -> int:
    rows = _query(
        real.psycopg,
        "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s AND backend_start >= to_timestamp(%s)",
        (PostgresRateLimiter.application_name, real.started),
    )
    return int(rows[0][0])


def test_the_connection_count_stays_constant_under_concurrent_checks(real):
    def burst() -> int:
        with ThreadPoolExecutor(max_workers=16) as threads:
            results = list(threads.map(lambda i: real.limiter.allow(f"{real.prefix}:{i % 50}", limit=10_000, window_seconds=60), range(300)))
        assert all(results)
        return _limiter_backends(real)

    first = burst()
    second = burst()

    assert 1 <= first <= 3
    assert second == first
    assert real.limiter._pool.stats()["created"] == first


def test_counts_reset_per_window_and_expired_windows_are_deleted(real):
    live, old = f"{real.prefix}:live", f"{real.prefix}:old"
    assert real.limiter.allow(old, limit=2, window_seconds=60)
    real.limiter.last_prune.result(timeout=10)  # the first check's prune, in the background
    real.clock[0] += 61

    assert [real.limiter.allow(live, limit=2, window_seconds=60) for _ in range(3)] == [True, True, False]

    # After the prune interval: live starts a new window, old is more than two windows old.
    real.clock[0] += real.limiter.prune_interval_seconds
    assert real.limiter.allow(live, limit=2, window_seconds=60) is True
    real.limiter.last_prune.result(timeout=10)
    keys = {key for (key,) in _query(real.psycopg, "SELECT key FROM omnidesk_api_resource_rate_limits WHERE key LIKE %s", (f"{real.prefix}:%",))}
    assert keys == {live}


def test_a_backlog_is_deleted_in_batches_within_the_statement_timeout(real):
    with real.psycopg.connect(DSN, autocommit=True) as con:
        con.execute(
            "INSERT INTO omnidesk_api_resource_rate_limits (key, window_started, count) "
            "SELECT %s::text || g::text, 0, 1 FROM generate_series(1, 12000) AS g",
            (f"{real.prefix}:backlog:",),
        )

    deleted = real.limiter._prune(cutoff=1.0)

    assert deleted >= 12000  # three batches of at most prune_batch_rows each
    left = _query(real.psycopg, "SELECT count(*) FROM omnidesk_api_resource_rate_limits WHERE key LIKE %s", (f"{real.prefix}:backlog:%",))
    assert left[0][0] == 0


def test_connections_carry_the_statement_and_lock_timeouts(real):
    settings = real.limiter._run(
        lambda cur: (cur.execute("SELECT current_setting('statement_timeout'), current_setting('lock_timeout'), current_setting('application_name')"), cur.fetchone())[1]
    )

    assert tuple(settings) == ("2s", "1s", PostgresRateLimiter.application_name)


def test_a_locked_bucket_fails_the_check_quickly_instead_of_hanging(real):
    key = f"{real.prefix}:locked"
    assert real.limiter.allow(key, limit=10, window_seconds=60)

    with real.psycopg.connect(DSN) as holder:
        holder.execute("SELECT 1 FROM omnidesk_api_resource_rate_limits WHERE key = %s FOR UPDATE", (key,))
        started = time.monotonic()
        with pytest.raises(real.psycopg.errors.LockNotAvailable):
            real.limiter.allow(key, limit=10, window_seconds=60)
        elapsed = time.monotonic() - started
        holder.rollback()

    assert elapsed < 5  # lock_timeout is 1 s; without it the check waits for the holder
    assert real.limiter.allow(key, limit=10, window_seconds=60) is True  # the connection is still usable


def test_a_slow_statement_is_cancelled_by_the_statement_timeout(real):
    started = time.monotonic()
    with pytest.raises(real.psycopg.errors.QueryCanceled):
        real.limiter._run(lambda cur: cur.execute("SELECT pg_sleep(10)"))

    assert time.monotonic() - started < 6  # statement_timeout is 2 s
