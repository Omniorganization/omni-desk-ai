from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

from fastapi import HTTPException, Request


_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9@._:/-]+")


@dataclass(frozen=True)
class ResourceGuardSnapshot:
    rate_buckets: int
    inflight_total: int
    inflight_by_class: dict[str, int]


class InMemoryRateLimiter:
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


class ApiResourceGuard:
    """Bound public API consumption before and after admin authentication."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.rate = InMemoryRateLimiter()
        self._lock = threading.Lock()
        self._inflight_total = 0
        self._inflight_by_class: dict[str, int] = {}

    def _enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", True))

    async def before_request(self, request: Request) -> Callable[[], None]:
        if not self._enabled():
            return lambda: None
        await self._check_body_size(request)
        client = _client_key(request)
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


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    host = forwarded or getattr(getattr(request, "client", None), "host", None) or "unknown"
    return _clean(host)


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
