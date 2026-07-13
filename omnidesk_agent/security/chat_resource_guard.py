from __future__ import annotations

from typing import Callable

from fastapi import Request

from omnidesk_agent.security.resource_guard import (
    ApiResourceGuard,
    _clean,
    _client_key,
    _path_key,
)


def route_class(path: str) -> str:
    if path == "/agent/run":
        return "agent"
    if path in {"/api/chat", "/api/chat/stream"}:
        return "chat"
    if path.startswith("/app/conversations/") and path.endswith("/ask"):
        return "chat"
    return "general"


class ChatAwareApiResourceGuard(ApiResourceGuard):
    """ApiResourceGuard with a static, import-order-independent route taxonomy."""

    async def before_request(self, request: Request) -> Callable[[], None]:
        if not self._enabled():
            return lambda: None
        await self._check_body_size(request)
        client = _client_key(request, self.cfg)
        path = _path_key(request.url.path)
        window = int(getattr(self.cfg, "window_seconds", 60))
        self._check_rate(
            f"ip:{client}",
            int(getattr(self.cfg, "max_requests_per_ip", 300)),
            window,
            "ip",
        )
        self._check_rate(
            f"ip-path:{client}:{path}",
            int(getattr(self.cfg, "max_requests_per_endpoint", 120)),
            window,
            "endpoint",
        )
        return self._acquire(route_class(request.url.path))

    def check_authenticated(self, request: Request, *, actor: str, role: str) -> None:
        if not self._enabled():
            return
        path = _path_key(request.url.path)
        classification = route_class(request.url.path)
        window = int(getattr(self.cfg, "window_seconds", 60))
        actor_key = _clean(actor or "unknown")
        role_key = _clean(role or "unknown")
        org_key = _clean(
            request.headers.get("x-omnidesk-org")
            or request.headers.get("x-omnidesk-organization")
            or "org_default"
        )
        self._check_rate(
            f"actor:{actor_key}",
            int(getattr(self.cfg, "max_requests_per_actor", 120)),
            window,
            "actor",
        )
        self._check_rate(
            f"role:{role_key}",
            int(getattr(self.cfg, "max_requests_per_role", 600)),
            window,
            "role",
        )
        self._check_rate(
            f"org:{org_key}:{path}",
            int(getattr(self.cfg, "max_requests_per_org_endpoint", 600)),
            window,
            "organization",
        )
        if classification == "agent":
            self._check_rate(
                f"actor-agent:{actor_key}",
                int(getattr(self.cfg, "agent_run_max_requests_per_actor", 20)),
                window,
                "agent",
            )
        elif classification == "chat":
            self._check_rate(
                f"actor-chat:{actor_key}",
                int(getattr(self.cfg, "chat_max_requests_per_actor", 60)),
                window,
                "chat",
            )
