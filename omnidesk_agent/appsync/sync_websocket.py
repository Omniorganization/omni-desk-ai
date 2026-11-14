"""Admission control and polling loop for the ``/app/ws`` sync feed.

HTTP requests pass the API resource guard in the server middleware; WebSocket
handshakes do not. This module spends the same pre-authentication IP budget as
HTTP requests (so a socket is no cheaper way to guess tokens), applies the
guard's authenticated actor/role limits, and caps concurrent connections per
actor and in total. It only uses the guard's public ``rate`` limiter and
``check_authenticated``.
"""

from __future__ import annotations

import asyncio
import re
import threading
from ipaddress import ip_address, ip_network
from typing import Any, Callable

from fastapi import HTTPException, WebSocket

WS_PATH = "/app/ws"
SEND_TIMEOUT_SECONDS = 10.0
# Polls share the store's bounded PostgreSQL pool with HTTP handlers, which wait
# for a free connection on the event loop; keep socket polls a small share of it.
MAX_CONCURRENT_SYNC_POLLS = 4
_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9@._:/-]+")


class WebSocketRejected(Exception):
    """The connection is refused; ``reason`` is a fixed, low-cardinality label."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def _clean(value: str) -> str:
    # Same key alphabet as the API resource guard, so both share one IP bucket.
    return _SAFE_KEY_RE.sub("_", str(value or "").strip())[:160] or "unknown"


def _trusted_proxy(host: str, trusted_proxy_ips: list[str]) -> bool:
    try:
        host_ip = ip_address(host)
    except ValueError:
        host_ip = None
    for raw in trusted_proxy_ips or []:
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


def client_key(websocket: WebSocket, trusted_proxy_ips: list[str]) -> str:
    host = str(getattr(getattr(websocket, "client", None), "host", None) or "unknown").strip()
    forwarded = str(websocket.headers.get("x-forwarded-for", "")).split(",", 1)[0].strip()
    if forwarded and _trusted_proxy(host, trusted_proxy_ips):
        host = forwarded
    return _clean(host)


def _guard_enabled(guard: Any) -> bool:
    return guard is not None and bool(getattr(getattr(guard, "cfg", None), "enabled", True))


def admit_handshake(guard: Any, websocket: WebSocket) -> None:
    """Charge one handshake to the client's pre-authentication budget."""
    if not _guard_enabled(guard):
        return
    cfg = guard.cfg
    client = client_key(websocket, list(getattr(cfg, "trusted_proxy_ips", []) or []))
    window = int(getattr(cfg, "window_seconds", 60))
    checks = (
        (f"ip:{client}", int(getattr(cfg, "max_requests_per_ip", 300))),
        (f"ip-path:{client}:{WS_PATH}", int(getattr(cfg, "max_requests_per_endpoint", 120))),
    )
    for key, limit in checks:
        if not guard.rate.allow(key, limit=limit, window_seconds=window):
            raise WebSocketRejected("rate_limited", "rate limit exceeded")


def admit_authenticated(guard: Any, websocket: WebSocket, *, actor: str, role: str) -> None:
    if not _guard_enabled(guard):
        return
    try:
        guard.check_authenticated(websocket, actor=actor, role=role)
    except HTTPException as exc:
        raise WebSocketRejected("rate_limited", str(exc.detail)) from exc


class WebSocketConnectionLimiter:
    """Per-process cap on open sync sockets, per actor and in total."""

    def __init__(self, *, max_per_actor: int, max_total: int) -> None:
        self.max_per_actor = max(1, int(max_per_actor))
        self.max_total = max(1, int(max_total))
        self._lock = threading.Lock()
        self._by_actor: dict[str, int] = {}
        self._total = 0

    def acquire(self, actor: str) -> Callable[[], None]:
        with self._lock:
            if self._total >= self.max_total:
                raise WebSocketRejected("connection_limit", "too many sync connections")
            current = self._by_actor.get(actor, 0)
            if current >= self.max_per_actor:
                raise WebSocketRejected("actor_connection_limit", "too many sync connections for this actor")
            self._by_actor[actor] = current + 1
            self._total += 1
        released = False

        def release() -> None:
            nonlocal released
            with self._lock:
                if released:
                    return
                released = True
                self._total = max(0, self._total - 1)
                remaining = self._by_actor.get(actor, 0) - 1
                if remaining > 0:
                    self._by_actor[actor] = remaining
                else:
                    self._by_actor.pop(actor, None)

        return release

    def active(self, actor: str | None = None) -> int:
        with self._lock:
            return self._total if actor is None else self._by_actor.get(actor, 0)


async def stream_sync_events(
    websocket: WebSocket,
    store: Any,
    *,
    actor: str,
    since_seq: int,
    poll_seconds: float,
    poll_slots: asyncio.Semaphore,
) -> None:
    """Push the actor's sync feed until the client goes away.

    ``sync_since`` runs in a worker thread so database reads never block the
    event loop. Client frames are not part of the protocol: they are read (to
    notice a disconnect) but never trigger an extra sync.
    """
    loop = asyncio.get_running_loop()
    receiver: asyncio.Future[Any] = asyncio.ensure_future(websocket.receive())
    try:
        while True:
            async with poll_slots:
                sync = await asyncio.to_thread(store.sync_since, since_seq, actor=actor)
            await asyncio.wait_for(websocket.send_json({"ok": True, **sync}), timeout=SEND_TIMEOUT_SECONDS)
            since_seq = int(sync["sync_seq"])
            deadline = loop.time() + poll_seconds
            while (remaining := deadline - loop.time()) > 0:
                done, _ = await asyncio.wait({receiver}, timeout=remaining)
                if not done:
                    break
                if receiver.result().get("type") == "websocket.disconnect":
                    return
                receiver = asyncio.ensure_future(websocket.receive())
    finally:
        receiver.cancel()
