from __future__ import annotations

from typing import Any, AsyncIterator

from omnidesk_agent.models.base import ModelDelta, ModelRequest


class CompletionOnlyStreamingRouter:
    """Adapt a governed complete-only runtime router before any delta is emitted."""

    def __init__(self, router: Any):
        self.router = router

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelDelta]:
        complete = getattr(self.router, "complete", None)
        if not callable(complete):
            raise RuntimeError("model router does not expose complete or stream policy")
        response = await complete(request)
        yield ModelDelta(
            sequence=1,
            provider=str(getattr(response, "provider", "unknown")),
            model=str(getattr(response, "model", "unknown")),
            profile=str(getattr(response, "profile", "unknown")),
            text=str(getattr(response, "text", "")),
            usage=getattr(response, "usage", None) or {},
            finish_reason="complete_fallback",
            provider_request_id=str(
                (getattr(response, "raw", None) or {}).get("id") or ""
            )
            or None,
            native=False,
        )
