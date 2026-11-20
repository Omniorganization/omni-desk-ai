"""The API guard keeps probes alive and does not hammer or block on its shared store.

Regression tests for the 2026-11-21 audit (operations: high; data-reliability:
high): /health returned 429 under load and went through the PostgreSQL limiter,
the limiter opened a fresh PostgreSQL connection for every check, synchronously
on the event loop, with no timeouts, and its table was never pruned. The same
behaviour against a real server is in test_postgres_rate_limiter_real.py.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from omnidesk_agent.config import ApiResourceGuardConfig
from omnidesk_agent.security.chat_resource_guard import ChatAwareApiResourceGuard
from omnidesk_agent.security.resource_guard import PROBE_PATHS, PostgresRateLimiter


class _FakeCursor:
    def __init__(self, con):
        self.con = con
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.con.statements.append(" ".join(sql.split())[:60])
        if self.con.fail_next:
            self.con.fail_next = False
            self.con.closed = True
            raise ConnectionError("server closed the connection")
        if self.con.registry.timeout_next:
            self.con.registry.timeout_next = False
            raise TimeoutError("canceling statement due to lock timeout")
        if "RETURNING count" in sql:
            self.con.registry.threads.append(threading.current_thread().name)
            key = params[0]
            self.con.counts[key] = self.con.counts.get(key, 0) + 1
            self._row = (self.con.counts[key],)
        elif "COUNT(*)" in sql:
            self._row = (len(self.con.counts),)

    def fetchone(self):
        return self._row


class _FakeConnection:
    def __init__(self, registry):
        self.registry = registry
        self.closed = False
        self.autocommit = False
        self.statements: list[str] = []
        self.counts = registry.counts
        self.fail_next = False

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def fake_psycopg(monkeypatch):
    registry = types.SimpleNamespace(connections=[], counts={}, dsns=[], threads=[], timeout_next=False, options="")
    lock = threading.Lock()

    def connect(dsn, **kwargs):
        con = _FakeConnection(registry)
        with lock:
            registry.connections.append(con)
            registry.dsns.append((dsn, kwargs))
        return con

    def make_conninfo(dsn, **kwargs):
        return " ".join([dsn, *(f"{k}={v!r}" for k, v in sorted(kwargs.items()))])

    conninfo = types.SimpleNamespace(
        conninfo_to_dict=lambda dsn: {"options": registry.options} if registry.options else {},
        make_conninfo=make_conninfo,
    )
    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=connect, conninfo=conninfo))
    monkeypatch.setenv("OMNIDESK_TEST_RATE_DSN", "postgresql://u:p@db/x")
    return registry


def test_postgres_limiter_counts_on_one_reused_connection(fake_psycopg):
    limiter = PostgresRateLimiter(dsn_env="OMNIDESK_TEST_RATE_DSN")

    results = [limiter.allow("ip:1", limit=3, window_seconds=60) for _ in range(5)]

    assert results == [True, True, True, False, False]
    assert len(fake_psycopg.connections) == 1  # the schema setup's connection, reused
    assert fake_psycopg.connections[-1].autocommit is True


def test_postgres_limiter_never_opens_more_than_its_pool(fake_psycopg):
    limiter = PostgresRateLimiter(dsn_env="OMNIDESK_TEST_RATE_DSN", pool_size=3)

    with ThreadPoolExecutor(max_workers=16) as threads:
        assert all(threads.map(lambda i: limiter.allow(f"ip:{i % 7}", limit=10_000, window_seconds=60), range(300)))
    limiter.close()

    assert 1 <= len(fake_psycopg.connections) <= 3


def test_postgres_limiter_connections_carry_timeouts_and_keep_dsn_options(fake_psycopg):
    fake_psycopg.options = "-c search_path=omnidesk"
    PostgresRateLimiter(dsn_env="OMNIDESK_TEST_RATE_DSN", statement_timeout_ms=1500, lock_timeout_ms=700).close()

    dsn, kwargs = fake_psycopg.dsns[0]
    assert "options='-c search_path=omnidesk -c statement_timeout=1500 -c lock_timeout=700'" in dsn
    assert "application_name='omnidesk-rate-limiter'" in dsn
    assert kwargs["connect_timeout"] >= 1


def test_postgres_limiter_does_not_retry_a_timeout(fake_psycopg):
    # A lock or statement timeout leaves the connection healthy: retrying would
    # only double the wait. The check fails and the request is refused.
    limiter = PostgresRateLimiter(dsn_env="OMNIDESK_TEST_RATE_DSN")
    fake_psycopg.timeout_next = True

    with pytest.raises(TimeoutError):
        limiter.allow("k", limit=10, window_seconds=60)

    assert len(fake_psycopg.connections) == 1 and not fake_psycopg.connections[0].closed
    assert limiter.allow("k", limit=10, window_seconds=60) is True


def test_postgres_limiter_replaces_a_broken_connection_once(fake_psycopg):
    limiter = PostgresRateLimiter(dsn_env="OMNIDESK_TEST_RATE_DSN")
    limiter.allow("k", limit=10, window_seconds=60)
    fake_psycopg.connections[-1].fail_next = True

    assert limiter.allow("k", limit=10, window_seconds=60) is True
    assert fake_psycopg.connections[-2].closed is True  # the broken one was dropped


def test_postgres_limiter_prunes_expired_windows(fake_psycopg):
    clock = [1000.0]
    limiter = PostgresRateLimiter(dsn_env="OMNIDESK_TEST_RATE_DSN", clock=lambda: clock[0])
    limiter.allow("k", limit=10, window_seconds=60)
    clock[0] += limiter.prune_interval_seconds + 1
    limiter.allow("k", limit=10, window_seconds=60)

    deletes = [s for con in fake_psycopg.connections for s in con.statements if s.startswith("DELETE FROM omnidesk_api_resource_rate_limits")]
    assert len(deletes) == 2  # first call, then again after the interval


class _ThreadRecordingLimiter:
    backend = "postgres"

    def __init__(self, fail: bool = False):
        self.threads: list[int] = []
        self.fail = fail

    def allow(self, key, *, limit, window_seconds):
        self.threads.append(threading.get_ident())
        if self.fail:
            raise RuntimeError("rate-limit store unavailable")
        return True

    def size(self):
        return 0


class _Request:
    def __init__(self, path: str, method: str = "GET"):
        self.method = method
        self.url = types.SimpleNamespace(path=path)
        self.client = types.SimpleNamespace(host="203.0.113.9")
        self.headers = types.SimpleNamespace(get=lambda *_a, **_k: None, getlist=lambda *_a: [])

    async def stream(self):
        yield b""


def test_guard_checks_run_on_the_limiters_own_bounded_workers(fake_psycopg):
    limiter = PostgresRateLimiter(dsn_env="OMNIDESK_TEST_RATE_DSN", pool_size=2)
    guard = ChatAwareApiResourceGuard(ApiResourceGuardConfig(max_body_bytes=0))
    guard.rate = limiter

    async def admit():
        release = await guard.before_request(_Request("/admin/status"))
        release()
        await guard.check_authenticated_async(_Request("/admin/status"), actor="a", role="viewer")

    asyncio.run(admit())
    limiter.close()

    assert fake_psycopg.threads and all(name.startswith("omnidesk-rate-limit-db") for name in fake_psycopg.threads)


def test_shared_store_checks_run_off_the_event_loop():
    guard = ChatAwareApiResourceGuard(ApiResourceGuardConfig(max_body_bytes=0))
    limiter = _ThreadRecordingLimiter()
    guard.rate = limiter

    async def admit():
        loop_thread = threading.get_ident()
        release = await guard.before_request(_Request("/admin/status"))
        release()
        await guard.check_authenticated_async(_Request("/admin/status"), actor="a", role="viewer")
        return loop_thread

    loop_thread = asyncio.run(admit())

    assert limiter.threads and loop_thread not in limiter.threads


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_liveness_probe_skips_rate_limits_and_concurrency_caps(method):
    guard = ChatAwareApiResourceGuard(ApiResourceGuardConfig(max_inflight_requests=1, max_body_bytes=0))
    guard.rate = _ThreadRecordingLimiter(fail=True)  # store down

    async def admit():
        held = guard._acquire("general")  # the only slot is taken
        try:
            release = await guard.before_request(_Request("/health", method))
            release()
        finally:
            held()

    asyncio.run(admit())
    assert PROBE_PATHS == {"/health"}


@pytest.mark.parametrize("path,method", [("/ready", "GET"), ("/health", "POST"), ("/health/", "GET"), ("/healthz", "GET")])
def test_only_the_exact_liveness_probe_is_exempt(path, method):
    guard = ChatAwareApiResourceGuard(ApiResourceGuardConfig(max_body_bytes=0))
    guard.rate = _ThreadRecordingLimiter(fail=True)

    with pytest.raises(RuntimeError, match="unavailable"):
        asyncio.run(guard.before_request(_Request(path, method)))


def test_health_answers_while_the_rate_store_is_down(tmp_path):
    from omnidesk_agent.config import AppConfig
    from omnidesk_agent.server import create_app

    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "ws"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.learning.growth_plan_file = tmp_path / "growth.json"
    cfg.channels.gmail.credentials_file = tmp_path / "g" / "c.json"
    cfg.channels.gmail.token_file = tmp_path / "g" / "t.json"
    cfg.plugins.enabled = False
    cfg.ensure_dirs()
    app = create_app(cfg)
    app.state.resource_guard.rate = _ThreadRecordingLimiter(fail=True)

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/openapi.json").status_code == 500  # everything else fails closed


def _readiness_app(tmp_path):
    from omnidesk_agent.config import AppConfig
    from omnidesk_agent.server import create_app

    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "ws"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.learning.growth_plan_file = tmp_path / "growth.json"
    cfg.channels.gmail.credentials_file = tmp_path / "g" / "c.json"
    cfg.channels.gmail.token_file = tmp_path / "g" / "t.json"
    cfg.gateway.admin_allowed_ips = ["testclient", "127.0.0.1"]
    cfg.plugins.enabled = False
    cfg.ensure_dirs()
    return create_app(cfg)


@pytest.mark.parametrize("status,ready", [
    ({"current": 7, "required": 7, "ready": True}, True),
    ({"current": 5, "required": 7, "ready": False}, False),
    (RuntimeError("relation does not exist"), False),
])
def test_readiness_reports_the_real_schema_version(tmp_path, monkeypatch, status, ready):
    """Operations audit: readiness hard-coded schema_version=True."""
    monkeypatch.setenv("OMNIDESK_ADMIN_TOKEN", "a" * 40)
    monkeypatch.setenv("OMNIDESK_GATEWAY_SECRET", "g" * 40)
    app = _readiness_app(tmp_path)

    def schema_status():
        if isinstance(status, Exception):
            raise status
        return status

    app.state.runtime.app_sync.schema_status = schema_status
    with TestClient(app, raise_server_exceptions=False) as client:
        deep = client.get("/admin/ready", headers={"authorization": "Bearer " + "a" * 40})

    checks = deep.json()["checks"] if deep.status_code == 200 else deep.json()["detail"]["checks"]
    assert checks["schema_version"] is ready
    assert checks["schema"]["versioned"] is True and checks["schema"]["ready"] is ready
