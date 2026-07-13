from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

from omnidesk_agent.appsync.chat_service import ChatTurnService
from omnidesk_agent.appsync.store import AppSyncStore
from omnidesk_agent.models.base import ModelDelta, ModelRequest
from omnidesk_agent.models.router import RoutePlan
from omnidesk_agent.models.router_streaming import GovernedStreamingRouter

ROOT = Path(__file__).resolve().parents[1]


def _request(*, idempotency_key: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if idempotency_key:
        headers.append((b"idempotency-key", idempotency_key.encode("utf-8")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/chat/stream",
            "raw_path": b"/api/chat/stream",
            "query_string": b"",
            "headers": headers,
            "client": ("testclient", 123),
            "server": ("testserver", 80),
        }
    )


def _service(tmp_path: Path, *, require_idempotency: bool = True) -> ChatTurnService:
    cfg = SimpleNamespace(
        app_sync=SimpleNamespace(require_idempotency=require_idempotency),
        api_resource_guard=SimpleNamespace(max_inflight_chat_requests=2),
    )
    runtime = SimpleNamespace(model_router=object())
    return ChatTurnService(
        cfg=cfg,
        runtime=runtime,
        store=AppSyncStore(tmp_path / "appsync.json"),
    )


def test_chat_idempotency_fingerprint_ignores_transport_stream_flag(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    legacy = service._idempotency_payload(
        {"content": "hello", "model_profile": "fast"},
        "conv-1",
    )
    non_stream = service._idempotency_payload(
        {"content": "hello", "model_profile": "fast", "stream": False},
        "conv-1",
    )
    stream = service._idempotency_payload(
        {"content": "hello", "model_profile": "fast", "stream": True},
        "conv-1",
    )
    assert legacy == non_stream == stream
    assert "stream" not in legacy


@pytest.mark.parametrize(
    ("payload", "idempotency_key", "status_code"),
    [
        ({}, "idem-1", 422),
        ({"content": "hello"}, None, 428),
        (
            {"conversation_id": "conv-missing", "content": "hello"},
            "idem-2",
            404,
        ),
    ],
)
def test_stream_preflight_rejects_invalid_writes_before_http_200(
    tmp_path: Path,
    payload: dict[str, object],
    idempotency_key: str | None,
    status_code: int,
) -> None:
    service = _service(tmp_path)
    with pytest.raises(HTTPException) as exc_info:
        service.prepare_stream(
            request=_request(idempotency_key=idempotency_key),
            payload=payload,
            actor="operator-1",
            role="operator",
        )
    assert exc_info.value.status_code == status_code


def test_stream_route_shares_limit_and_never_drops_normal_terminator() -> None:
    source = (ROOT / "omnidesk_agent/appsync/chat_routes.py").read_text(
        encoding="utf-8"
    )
    assert "stream_service.stream_limit = service.stream_limit" in source
    assert "await queue.put(None)" in source
    assert "put_nowait(None)" not in source
    assert "prepared = stream_service.prepare_stream" in source


class _Provider:
    provider_name = "test"
    model = "test-model"
    profile_name = "fast"
    settings = SimpleNamespace(max_output_tokens=32)


class _Decision:
    allowed = True
    cache_key = ""
    truncated_system = None
    truncated_user = None
    estimated_input_tokens = 2
    budget_overridden = False
    reason = "within-budget"


class _TokenBudget:
    def decide(self, **_kwargs):
        return _Decision()

    def get_cached(self, _key: str):
        return None

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text.split()))

    def record_call(self, **_kwargs):
        raise AssertionError("failed streams must not be recorded as successful calls")

    def put_cached(self, **_kwargs):
        raise AssertionError("failed streams must not be cached")


class _Ledger:
    def record(self, **_kwargs):
        raise AssertionError("failed streams must not enter the cost ledger as success")


class _Pricing:
    def estimate(self, **_kwargs) -> float:
        return 0.0


class _Router:
    def __init__(self):
        self.providers = {"fast": _Provider()}
        self.token_budget = _TokenBudget()
        self.cost_ledger = _Ledger()
        self.pricing_table = _Pricing()
        self.error_counts: dict[str, int] = {}
        self.cfg = SimpleNamespace(max_output_tokens=32)
        self.failures: list[str] = []
        self.successes: list[str] = []

    def route_plan(self, _task: str, _metadata):
        return RoutePlan(profiles=["fast"], max_retries=0)

    def _offline_forbids_profile(self, _profile, _provider):
        return False

    def _circuit_open(self, _profile, _plan):
        return False

    def _check_budget(self, _request, *, profile, provider):
        assert profile == "fast"
        assert provider is self.providers["fast"]
        return None

    def _record_failure(self, profile: str):
        self.failures.append(profile)

    def _record_success(self, profile: str):
        self.successes.append(profile)

    async def complete(self, _request: ModelRequest):
        raise AssertionError("partial failed streams must not fall back")


@pytest.mark.parametrize("finish_reason", ["failed", "incomplete", "error"])
@pytest.mark.asyncio
async def test_failed_provider_terminal_is_not_persisted_as_success(
    monkeypatch: pytest.MonkeyPatch,
    finish_reason: str,
) -> None:
    async def failed_stream(_provider, _request):
        yield ModelDelta(
            sequence=1,
            provider="test",
            model="test-model",
            profile="fast",
            text="partial",
            native=True,
        )
        yield ModelDelta(
            sequence=2,
            provider="test",
            model="test-model",
            profile="fast",
            finish_reason=finish_reason,
            provider_request_id="req-failed",
            native=True,
        )

    monkeypatch.setattr(
        "omnidesk_agent.models.router_streaming.stream_provider",
        failed_stream,
    )
    router = _Router()
    adapter = GovernedStreamingRouter(router)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="partial delivery"):
        _ = [
            delta
            async for delta in adapter.stream(
                ModelRequest(
                    system="system",
                    user="hello",
                    task="chat",
                    task_id="task-failed-stream",
                    metadata={"actor": "operator-1"},
                )
            )
        ]

    assert router.failures == ["fast"]
    assert router.successes == []
