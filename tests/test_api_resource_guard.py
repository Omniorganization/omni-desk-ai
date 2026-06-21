from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from omnidesk_agent.config import ApiResourceGuardConfig, AppConfig
from omnidesk_agent.security.resource_guard import (
    ApiResourceGuard,
    InMemoryRateLimiter,
    PostgresRateLimiter,
    SQLiteRateLimiter,
    _build_rate_limiter,
    _clean,
    _path_key,
)
from omnidesk_agent.storage.sqlite import connect_sqlite
from omnidesk_agent.validation.production import validate_production_config


def _request(path: str, *, method: str = "POST", headers: dict[str, str] | None = None, body: bytes = b"") -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    raw_headers = [(key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("203.0.113.10", 12345),
            "path": path,
            "headers": raw_headers,
        },
        receive,
    )


def test_in_memory_rate_limiter_resets_and_collects_expired_buckets():
    now = 0.0
    limiter = InMemoryRateLimiter(clock=lambda: now)

    assert not limiter.allow("ip:alice", limit=0, window_seconds=60)
    assert limiter.allow("ip:alice", limit=1, window_seconds=1)
    assert not limiter.allow("ip:alice", limit=1, window_seconds=1)
    now = 2.0
    assert limiter.allow("ip:alice", limit=1, window_seconds=1)
    assert limiter.size() == 1

    limiter._buckets.update({f"expired:{idx}": (0.0, 1) for idx in range(4097)})
    assert limiter.allow("ip:bob", limit=2, window_seconds=1)
    assert "expired:0" not in limiter._buckets


def test_sqlite_rate_limiter_persists_window_counts(tmp_path):
    db_path = tmp_path / "resource_guard.sqlite3"
    first = SQLiteRateLimiter(db_path)
    second = SQLiteRateLimiter(db_path)

    assert first.allow("ip:alice", limit=1, window_seconds=60)
    assert not second.allow("ip:alice", limit=1, window_seconds=60)
    assert second.allow("ip:bob", limit=2, window_seconds=60)
    assert second.allow("ip:bob", limit=2, window_seconds=60)
    assert second.size() == 2


def test_sqlite_rate_limiter_resets_expired_windows_and_garbage_collects(tmp_path):
    now = 10.0
    db_path = tmp_path / "resource_guard.sqlite3"
    limiter = SQLiteRateLimiter(db_path, clock=lambda: now)

    assert not limiter.allow("ip:blocked", limit=0, window_seconds=60)
    assert limiter.allow("ip:alice", limit=1, window_seconds=1)
    now = 12.0
    assert limiter.allow("ip:alice", limit=1, window_seconds=1)

    with connect_sqlite(db_path) as con:
        con.executemany(
            "INSERT OR REPLACE INTO api_resource_rate_limits(key, window_started, count) VALUES (?, ?, ?)",
            [(f"old:{idx}", 0.0, 1) for idx in range(4096)],
        )
    assert limiter.allow("ip:bob", limit=2, window_seconds=1)
    assert limiter.allow("ip:bob", limit=2, window_seconds=1)
    with connect_sqlite(db_path) as con:
        remaining_old = con.execute("SELECT COUNT(*) FROM api_resource_rate_limits WHERE key LIKE 'old:%'").fetchone()[0]
    assert remaining_old == 0


class _FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        self.connection.executed.append((query, params))
        if "RETURNING count" in query:
            self.row = (self.connection.returning_count,)
        elif "SELECT COUNT" in query:
            self.row = (self.connection.size_count,)

    def fetchone(self):
        return self.row


class _FakeConnection:
    def __init__(self):
        self.executed = []
        self.returning_count = 1
        self.size_count = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _FakeCursor(self)


def test_postgres_rate_limiter_uses_configured_dsn_and_limit_results(monkeypatch):
    connection = _FakeConnection()
    seen_dsns = []
    monkeypatch.setenv("OMNI_TEST_POSTGRES_DSN", "postgresql://user:pass@db/omnidesk")
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda dsn: seen_dsns.append(dsn) or connection))

    limiter = PostgresRateLimiter(dsn_env="OMNI_TEST_POSTGRES_DSN", clock=lambda: 100.0)

    assert "CREATE TABLE IF NOT EXISTS omnidesk_api_resource_rate_limits" in connection.executed[0][0]
    assert seen_dsns == ["postgresql://user:pass@db/omnidesk"]
    assert not limiter.allow("ip:blocked", limit=0, window_seconds=60)

    connection.returning_count = 1
    assert limiter.allow("ip:alice", limit=1, window_seconds=60)
    connection.returning_count = 2
    assert not limiter.allow("ip:alice", limit=1, window_seconds=60)
    connection.size_count = 3
    assert limiter.size() == 3


def test_postgres_rate_limiter_requires_dsn(monkeypatch):
    monkeypatch.delenv("OMNI_TEST_POSTGRES_DSN", raising=False)
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda _dsn: _FakeConnection()))

    with pytest.raises(RuntimeError, match="OMNI_TEST_POSTGRES_DSN is required"):
        PostgresRateLimiter(dsn_env="OMNI_TEST_POSTGRES_DSN")


def test_api_resource_guard_config_exposes_shared_backends(tmp_path):
    cfg = ApiResourceGuardConfig(backend="sqlite", sqlite_path=tmp_path / "guard.sqlite3")

    assert cfg.backend == "sqlite"
    assert cfg.sqlite_path.name == "guard.sqlite3"
    assert cfg.postgres_dsn_env == "OMNIDESK_POSTGRES_DSN"


def test_api_resource_guard_handles_disabled_and_body_size_branches():
    disabled = ApiResourceGuardConfig(enabled=False)
    guard = ApiResourceGuard(disabled)
    release = asyncio.run(guard.before_request(_request("/api/chat", body=b"too large")))
    release()
    guard.check_authenticated(_request("/api/chat"), actor="alice", role="operator")

    cfg = ApiResourceGuardConfig(max_body_bytes=3)
    guard = ApiResourceGuard(cfg)
    asyncio.run(guard.before_request(_request("/api/chat", method="GET", body=b"ignored")))

    with pytest.raises(HTTPException) as bad_length:
        asyncio.run(guard.before_request(_request("/api/chat", headers={"content-length": "not-a-number"})))
    assert bad_length.value.status_code == 400

    with pytest.raises(HTTPException) as too_large_header:
        asyncio.run(guard.before_request(_request("/api/chat", headers={"content-length": "4"})))
    assert too_large_header.value.status_code == 413

    with pytest.raises(HTTPException) as too_large_body:
        asyncio.run(guard.before_request(_request("/api/chat", body=b"four")))
    assert too_large_body.value.status_code == 413


def test_api_resource_guard_enforces_concurrency_and_reports_snapshot():
    cfg = ApiResourceGuardConfig(max_inflight_requests=1, max_inflight_chat_requests=1)
    guard = ApiResourceGuard(cfg)

    release = guard._acquire("general")
    with pytest.raises(HTTPException) as global_limited:
        guard._acquire("general")
    assert global_limited.value.detail == "global concurrency limit exceeded"
    snapshot = guard.snapshot()
    assert snapshot.backend == "memory"
    assert snapshot.inflight_total == 1
    release()
    release()
    assert guard.snapshot().inflight_total == 0

    class_guard = ApiResourceGuard(ApiResourceGuardConfig(max_inflight_requests=10, max_inflight_chat_requests=1))
    chat_release = class_guard._acquire("chat")
    with pytest.raises(HTTPException) as class_limited:
        class_guard._acquire("chat")
    assert class_limited.value.detail == "chat concurrency limit exceeded"
    chat_release()


def test_api_resource_guard_rate_helpers_and_backend_validation(tmp_path):
    cfg = ApiResourceGuardConfig(max_requests_per_actor=2, chat_max_requests_per_actor=1)
    guard = ApiResourceGuard(cfg)
    request = _request("/app/conversations/123456789abc/ask", headers={"x-omnidesk-org": "sales org!"})

    guard.check_authenticated(request, actor="alice@example.com", role="operator")
    with pytest.raises(HTTPException) as chat_limited:
        guard.check_authenticated(request, actor="alice@example.com", role="operator")
    assert chat_limited.value.detail == "chat rate limit exceeded"

    assert _path_key("/app/tasks/123456789abc/status") == "/app/tasks/{id}/status"
    assert _path_key("/app/tasks/abcdef-abcdef/status") == "/app/tasks/{id}/status"
    assert _clean(" bad!* ") == "bad_"
    assert _build_rate_limiter(ApiResourceGuardConfig(backend="sqlite", sqlite_path=tmp_path / "guard.sqlite3")).backend == "sqlite"
    with pytest.raises(ValueError, match="unsupported api_resource_guard.backend"):
        _build_rate_limiter(SimpleNamespace(backend="redis"))


def test_multi_instance_production_rejects_memory_resource_guard():
    cfg = AppConfig()
    cfg.plugins.enabled = False
    cfg.memory_privacy.encrypt_at_rest = True
    cfg.storage.backend = "postgres"
    cfg.storage.require_multi_instance_safe = True
    cfg.app_sync.backend = "postgres"
    cfg.api_resource_guard.backend = "memory"

    result = validate_production_config(
        cfg,
        {
            "OMNIDESK_ENV": "production",
            "OMNIDESK_ADMIN_TOKEN": "x" * 40,
            "OMNIDESK_GATEWAY_SECRET": "x" * 40,
            "OMNIDESK_MEMORY_ENCRYPTION_KEY": "x" * 40,
            "OMNIDESK_POSTGRES_DSN": "postgresql://user:pass@db/omnidesk",
            "OMNIDESK_APPSYNC_POSTGRES_DSN": "postgresql://user:pass@db/omnidesk",
        },
    )

    assert "api_resource_guard.backend must be postgres when storage.require_multi_instance_safe=true" in result["issues"]
