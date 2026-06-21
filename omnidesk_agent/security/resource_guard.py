from __future__ import annotations

import os
import re
import threading
import time
from ipaddress import ip_address, ip_network
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import HTTPException, Request

from omnidesk_agent.storage.sqlite import connect_sqlite


_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9@._:/-]+")


@dataclass(frozen=True)
class ResourceGuardSnapshot:
    backend: str
    rate_buckets: int
    inflight_total: int
    inflight_by_class: dict[str, int]


class InMemoryRateLimiter:
    backend = "memory"

    def __init__(self, clock: Callable[[], float] | None = None):
        self.clock = clock or time.time
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, int]] = {}

    def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        if limit <= 0:
            return False
        now = self.clock()
        window_seconds = max(1, int(window_seconds))
        with self._lock:
            started, count = self._buckets.get(key, (now, 0))
            if now - started >= window_seconds:
                started, count = now, 0
            if count >= limit:
                return False
            self._buckets[key] = (started, count + 1)
            self._gc(now, window_seconds)
            return True

    def _gc(self, now: float, window_seconds: int) -> None:
        if len(self._buckets) < 4096:
            return
        expired = [key for key, (started, _count) in self._buckets.items() if now - started >= window_seconds]
        for key in expired:
            self._buckets.pop(key, None)

    def size(self) -> int:
        with self._lock:
            return len(self._buckets)


class SQLiteRateLimiter:
    backend = "sqlite"

    def __init__(self, db_path: Path, clock: Callable[[], float] | None = None):
        self.db_path = db_path.expanduser()
        self.clock = clock or time.time
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS api_resource_rate_limits (
                    key TEXT PRIMARY KEY,
                    window_started REAL NOT NULL,
                    count INTEGER NOT NULL
                )
                """
            )

    def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        if limit <= 0:
            return False
        now = self.clock()
        window_seconds = max(1, int(window_seconds))
        with connect_sqlite(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT window_started, count FROM api_resource_rate_limits WHERE key = ?", (key,)).fetchone()
            if not row or now - float(row[0]) >= window_seconds:
                con.execute(
                    "INSERT OR REPLACE INTO api_resource_rate_limits(key, window_started, count) VALUES (?, ?, ?)",
                    (key, now, 1),
                )
                return True
            count = int(row[1])
            if count >= limit:
                return False
            con.execute("UPDATE api_resource_rate_limits SET count = ? WHERE key = ?", (count + 1, key))
            self._gc(con, now, window_seconds)
            return True

    def _gc(self, con, now: float, window_seconds: int) -> None:
        row = con.execute("SELECT COUNT(*) FROM api_resource_rate_limits").fetchone()
        if int(row[0]) < 4096:
            return
        con.execute("DELETE FROM api_resource_rate_limits WHERE window_started <= ?", (now - window_seconds,))

    def size(self) -> int:
        with connect_sqlite(self.db_path) as con:
            row = con.execute("SELECT COUNT(*) FROM api_resource_rate_limits").fetchone()
            return int(row[0])


class PostgresRateLimiter:
    backend = "postgres"

    def __init__(self, *, dsn_env: str, clock: Callable[[], float] | None = None):
        self.dsn_env = dsn_env
        self.clock = clock or time.time
        self._init_schema()

    def _dsn(self) -> str:
        dsn = os.getenv(self.dsn_env, "")
        if not dsn:
            raise RuntimeError(f"{self.dsn_env} is required when api_resource_guard.backend=postgres")
        return dsn

    def _connect(self):  # type: ignore[no-untyped-def]
        try:
            import psycopg  # type: ignore
        except Exception as exc:  # pragma: no cover - optional production dependency
            raise RuntimeError("Install psycopg to use api_resource_guard.backend=postgres") from exc
        return psycopg.connect(self._dsn())

    def _init_schema(self) -> None:
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS omnidesk_api_resource_rate_limits (
                        key TEXT PRIMARY KEY,
                        window_started DOUBLE PRECISION NOT NULL,
                        count INTEGER NOT NULL
                    )
                    """
                )

    def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        if limit <= 0:
            return False
        now = self.clock()
        window_seconds = max(1, int(window_seconds))
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO omnidesk_api_resource_rate_limits(key, window_started, count)
                    VALUES (%s, %s, 1)
                    ON CONFLICT (key) DO UPDATE SET
                        window_started = CASE
                            WHEN %s - omnidesk_api_resource_rate_limits.window_started >= %s THEN %s
                            ELSE omnidesk_api_resource_rate_limits.window_started
                        END,
                        count = CASE
                            WHEN %s - omnidesk_api_resource_rate_limits.window_started >= %s THEN 1
                            ELSE omnidesk_api_resource_rate_limits.count + 1
                        END
                    RETURNING count
                    """,
                    (key, now, now, window_seconds, now, now, window_seconds),
                )
                row = cur.fetchone()
                return int(row[0]) <= limit

    def size(self) -> int:
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM omnidesk_api_resource_rate_limits")
                row = cur.fetchone()
                return int(row[0])


class ApiResourceGuard:
    """Bound public API consumption before and after admin authentication."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.rate = _build_rate_limiter(cfg)
        self._lock = threading.Lock()
        self._inflight_total = 0
        self._inflight_by_class: dict[str, int] = {}

    def _enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", True))

    async def before_request(self, request: Request) -> Callable[[], None]:
        if not self._enabled():
            return lambda: None
        await self._check_body_size(request)
        client = _client_key(request, self.cfg)
        path = _path_key(request.url.path)
        window = int(getattr(self.cfg, "window_seconds", 60))
        self._check_rate(f"ip:{client}", int(getattr(self.cfg, "max_requests_per_ip", 300)), window, "ip")
        self._check_rate(f"ip-path:{client}:{path}", int(getattr(self.cfg, "max_requests_per_endpoint", 120)), window, "endpoint")
        return self._acquire(_route_class(request.url.path))

    def check_authenticated(self, request: Request, *, actor: str, role: str) -> None:
        if not self._enabled():
            return
        path = _path_key(request.url.path)
        route_class = _route_class(request.url.path)
        window = int(getattr(self.cfg, "window_seconds", 60))
        actor_key = _clean(actor or "unknown")
        role_key = _clean(role or "unknown")
        org_key = _clean(request.headers.get("x-omnidesk-org") or request.headers.get("x-omnidesk-organization") or "org_default")
        self._check_rate(f"actor:{actor_key}", int(getattr(self.cfg, "max_requests_per_actor", 120)), window, "actor")
        self._check_rate(f"role:{role_key}", int(getattr(self.cfg, "max_requests_per_role", 600)), window, "role")
        self._check_rate(f"org:{org_key}:{path}", int(getattr(self.cfg, "max_requests_per_org_endpoint", 600)), window, "organization")
        if route_class == "agent":
            self._check_rate(f"actor-agent:{actor_key}", int(getattr(self.cfg, "agent_run_max_requests_per_actor", 20)), window, "agent")
        elif route_class == "chat":
            self._check_rate(f"actor-chat:{actor_key}", int(getattr(self.cfg, "chat_max_requests_per_actor", 60)), window, "chat")

    async def _check_body_size(self, request: Request) -> None:
        max_bytes = int(getattr(self.cfg, "max_body_bytes", 1_048_576))
        if max_bytes <= 0 or request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
            return
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    raise HTTPException(status_code=413, detail="request body too large")
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid content-length")
        body = await request.body()
        if len(body) > max_bytes:
            raise HTTPException(status_code=413, detail="request body too large")

    def _check_rate(self, key: str, limit: int, window_seconds: int, label: str) -> None:
        if not self.rate.allow(key, limit=limit, window_seconds=window_seconds):
            raise HTTPException(status_code=429, detail=f"{label} rate limit exceeded")

    def _acquire(self, route_class: str) -> Callable[[], None]:
        global_limit = int(getattr(self.cfg, "max_inflight_requests", 64))
        class_limit = _class_limit(self.cfg, route_class)
        with self._lock:
            if self._inflight_total >= global_limit:
                raise HTTPException(status_code=429, detail="global concurrency limit exceeded")
            current_class = self._inflight_by_class.get(route_class, 0)
            if current_class >= class_limit:
                raise HTTPException(status_code=429, detail=f"{route_class} concurrency limit exceeded")
            self._inflight_total += 1
            self._inflight_by_class[route_class] = current_class + 1

        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            with self._lock:
                self._inflight_total = max(0, self._inflight_total - 1)
                current = max(0, self._inflight_by_class.get(route_class, 0) - 1)
                if current:
                    self._inflight_by_class[route_class] = current
                else:
                    self._inflight_by_class.pop(route_class, None)

        return release

    def snapshot(self) -> ResourceGuardSnapshot:
        with self._lock:
            return ResourceGuardSnapshot(
                backend=getattr(self.rate, "backend", "unknown"),
                rate_buckets=self.rate.size(),
                inflight_total=self._inflight_total,
                inflight_by_class=dict(self._inflight_by_class),
            )


def _class_limit(cfg, route_class: str) -> int:
    if route_class == "agent":
        return int(getattr(cfg, "max_inflight_agent_runs", 4))
    if route_class == "chat":
        return int(getattr(cfg, "max_inflight_chat_requests", 8))
    return int(getattr(cfg, "max_inflight_requests", 64))


def _build_rate_limiter(cfg):
    backend = str(getattr(cfg, "backend", "memory") or "memory").strip().lower()
    if backend == "memory":
        return InMemoryRateLimiter()
    if backend == "sqlite":
        return SQLiteRateLimiter(Path(getattr(cfg, "sqlite_path")))
    if backend == "postgres":
        return PostgresRateLimiter(dsn_env=str(getattr(cfg, "postgres_dsn_env", "OMNIDESK_POSTGRES_DSN")))
    raise ValueError(f"unsupported api_resource_guard.backend: {backend!r}")


def _client_key(request: Request, cfg=None) -> str:
    host = getattr(getattr(request, "client", None), "host", None) or "unknown"
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    if forwarded and _is_trusted_proxy(host, getattr(cfg, "trusted_proxy_ips", []) if cfg is not None else []):
        host = forwarded
    return _clean(host)


def _is_trusted_proxy(host: str, trusted_proxy_ips: list[str]) -> bool:
    if not trusted_proxy_ips:
        return False
    host = str(host or "").strip()
    try:
        host_ip = ip_address(host)
    except ValueError:
        host_ip = None
    for raw in trusted_proxy_ips:
        candidate = str(raw or "").strip()
        if not candidate:
            continue
        if host == candidate:
            return True
        if host_ip is None:
            continue
        try:
            if host_ip in ip_network(candidate, strict=False):
                return True
        except ValueError:
            continue
    return False


def _path_key(path: str) -> str:
    parts: list[str] = []
    for part in str(path or "/").split("/"):
        if not part:
            continue
        if _looks_dynamic(part):
            parts.append("{id}")
        else:
            parts.append(_clean(part))
    return "/" + "/".join(parts)


def _route_class(path: str) -> str:
    if path == "/agent/run":
        return "agent"
    if path == "/api/chat" or (path.startswith("/app/conversations/") and path.endswith("/ask")):
        return "chat"
    return "general"


def _looks_dynamic(part: str) -> bool:
    if len(part) >= 12 and any(ch.isdigit() for ch in part):
        return True
    return bool(re.fullmatch(r"[0-9a-fA-F-]{12,}", part))


def _clean(value: str) -> str:
    cleaned = _SAFE_KEY_RE.sub("_", str(value or "").strip())
    return cleaned[:160] or "unknown"
