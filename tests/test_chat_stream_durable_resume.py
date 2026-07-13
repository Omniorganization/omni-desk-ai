from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

from omnidesk_agent.appsync.chat_service import ChatStreamEvent, ChatTurnService
from omnidesk_agent.appsync.store import AppSyncStore
from omnidesk_agent.models.base import ModelRequest, ModelResponse
from omnidesk_agent.models.router import RoutePlan


def _request(key: str = "stream-idem") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/chat/stream",
            "raw_path": b"/api/chat/stream",
            "query_string": b"",
            "headers": [(b"idempotency-key", key.encode("utf-8"))],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
        }
    )


class _AllowedRouter:
    def route_plan(self, _task: str, _metadata: dict[str, object]) -> RoutePlan:
        return RoutePlan(profiles=["fast"], max_retries=0)


class _DeniedRouter:
    def route_plan(self, _task: str, _metadata: dict[str, object]) -> RoutePlan:
        raise PermissionError("profile denied")


class _TenantDeniedRouter:
    def route_plan(self, _task: str, metadata: dict[str, object]) -> RoutePlan:
        if metadata.get("organization_id") == "org_default":
            raise PermissionError("tenant profile denied")
        return RoutePlan(profiles=["restricted"], max_retries=0)


def _service(tmp_path: Path, router: object) -> ChatTurnService:
    cfg = SimpleNamespace(
        app_sync=SimpleNamespace(require_idempotency=True),
        api_resource_guard=SimpleNamespace(max_inflight_chat_requests=2),
    )
    return ChatTurnService(
        cfg=cfg,
        runtime=SimpleNamespace(model_router=router),
        store=AppSyncStore(tmp_path / "appsync.json"),
    )


def _conversation(service: ChatTurnService, actor: str = "operator-1") -> str:
    return str(
        service.store.create_conversation(
            actor=actor,
            title="Durable stream",
        )["conversation_id"]
    )


def test_explicit_profile_acl_is_checked_before_user_message_persistence(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, _DeniedRouter())
    conversation_id = _conversation(service)

    with pytest.raises(HTTPException) as exc_info:
        service.prepare_stream(
            request=_request(),
            payload={
                "conversation_id": conversation_id,
                "content": "hello",
                "model_profile": "restricted",
            },
            actor="operator-1",
            role="operator",
        )

    assert exc_info.value.status_code == 403
    assert service.store.list_messages(conversation_id, actor="operator-1") == []


def test_tenant_profile_acl_receives_org_before_message_persistence(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, _TenantDeniedRouter())
    conversation_id = _conversation(service)

    with pytest.raises(HTTPException) as exc_info:
        service.prepare_stream(
            request=_request("tenant-denied"),
            payload={
                "conversation_id": conversation_id,
                "content": "hello",
                "model_profile": "restricted",
            },
            actor="operator-1",
            role="operator",
        )

    assert exc_info.value.status_code == 403
    assert service.store.list_messages(conversation_id, actor="operator-1") == []


def test_in_progress_cached_stream_is_rejected_before_http_200(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, _AllowedRouter())
    conversation_id = _conversation(service)
    payload = {"conversation_id": conversation_id, "content": "hello"}
    service.prepare_stream(
        request=_request("in-progress"),
        payload=payload,
        actor="operator-1",
        role="operator",
    )
    message_count = len(
        service.store.list_messages(conversation_id, actor="operator-1")
    )

    with pytest.raises(HTTPException) as exc_info:
        service.prepare_stream(
            request=_request("in-progress"),
            payload=payload,
            actor="operator-1",
            role="operator",
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "stream_in_progress"
    assert len(
        service.store.list_messages(conversation_id, actor="operator-1")
    ) == message_count


def test_resume_without_reserved_state_is_rejected_without_second_message(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, _AllowedRouter())
    conversation_id = _conversation(service)

    with pytest.raises(HTTPException) as exc_info:
        service.prepare_stream(
            request=_request("missing-state"),
            payload={"conversation_id": conversation_id, "content": "hello"},
            actor="operator-1",
            role="operator",
            last_event_id=2,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "stream_resume_state_missing"
    assert service.store.list_messages(conversation_id, actor="operator-1") == []


@pytest.mark.asyncio
async def test_cached_stream_replay_preserves_original_event_ids_and_boundaries(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, _AllowedRouter())
    cached = {
        "conversation_id": "conv-1",
        "stream_status": "completed",
        "stream_events": [
            {
                "sequence": 1,
                "event": "chat.started",
                "data": {"conversation_id": "conv-1"},
            },
            {
                "sequence": 2,
                "event": "chat.delta",
                "data": {"text": "hel"},
            },
            {
                "sequence": 3,
                "event": "chat.delta",
                "data": {"text": "lo"},
            },
            {
                "sequence": 4,
                "event": "chat.completed",
                "data": {"native": True},
            },
        ],
    }

    replayed = [
        event
        async for event in service._replay_cached(cached, last_event_id=2)
    ]

    assert [(event.sequence, event.event) for event in replayed] == [
        (3, "chat.delta"),
        (4, "chat.completed"),
    ]
    assert replayed[0].data == {"text": "lo"}


@pytest.mark.asyncio
async def test_interrupted_stream_replays_failure_without_new_model_turn(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, _AllowedRouter())
    conversation_id = _conversation(service)
    payload = {"conversation_id": conversation_id, "content": "hello"}
    prepared = service.prepare_stream(
        request=_request("resume-idem"),
        payload=payload,
        actor="operator-1",
        role="operator",
    )
    service._append_stream_event(
        actor="operator-1",
        prepared=prepared,
        event=ChatStreamEvent(
            1,
            "chat.started",
            {"conversation_id": conversation_id},
        ),
    )
    service._append_stream_event(
        actor="operator-1",
        prepared=prepared,
        event=ChatStreamEvent(
            2,
            "chat.failed",
            {"code": "stream_interrupted"},
        ),
        status="interrupted",
    )
    message_count = len(
        service.store.list_messages(conversation_id, actor="operator-1")
    )

    resumed = service.prepare_stream(
        request=_request("resume-idem"),
        payload=payload,
        actor="operator-1",
        role="operator",
        last_event_id=1,
    )
    replayed = [
        event
        async for event in service._replay_cached(
            resumed.cached or {},
            last_event_id=1,
        )
    ]

    assert [(event.sequence, event.event) for event in replayed] == [
        (2, "chat.failed")
    ]
    assert replayed[0].data["code"] == "stream_interrupted"
    assert len(service.store.list_messages(conversation_id, actor="operator-1")) == message_count


class _StructuredProvider:
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


class _Budget:
    def decide(self, **_kwargs):
        return _Decision()

    def get_cached(self, _key: str):
        return None

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text.split()))

    def record_call(self, **_kwargs):
        return None

    def put_cached(self, **_kwargs):
        return None


class _Ledger:
    def record(self, **_kwargs):
        return None


class _Pricing:
    def estimate(self, **_kwargs) -> float:
        return 0.0


class _StructuredRouter:
    def __init__(self):
        self.providers = {"fast": _StructuredProvider()}
        self.token_budget = _Budget()
        self.cost_ledger = _Ledger()
        self.pricing_table = _Pricing()
        self.error_counts: dict[str, int] = {}
        self.cfg = SimpleNamespace(max_output_tokens=32)

    def route_plan(self, _task: str, _metadata: dict[str, object]) -> RoutePlan:
        return RoutePlan(profiles=["fast"], max_retries=0)

    def _offline_forbids_profile(self, _profile, _provider):
        return False

    def _circuit_open(self, _profile, _plan):
        return False

    def _check_budget(self, _request, *, profile, provider):
        return None

    def _record_failure(self, _profile):
        return None

    def _record_success(self, _profile):
        return None

    async def complete(self, _request: ModelRequest):
        raise AssertionError("native structured stream should not fall back")

    async def _repair_structured_output_if_needed(
        self,
        _request: ModelRequest,
        profile: str,
        _provider: object,
        _response: ModelResponse,
    ) -> ModelResponse:
        return ModelResponse(
            text='{"ok":true}',
            provider="test",
            model="test-model",
            profile=profile,
            usage={"output_tokens": 3},
            raw={"id": "repair-1"},
        )


@pytest.mark.asyncio
async def test_structured_stream_emits_only_validated_repaired_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def invalid_json_stream(_provider, _request):
        from omnidesk_agent.models.base import ModelDelta

        yield ModelDelta(
            sequence=1,
            provider="test",
            model="test-model",
            profile="fast",
            text='{"ok":',
            native=True,
        )
        yield ModelDelta(
            sequence=2,
            provider="test",
            model="test-model",
            profile="fast",
            text="INVALID}",
            native=True,
        )
        yield ModelDelta(
            sequence=3,
            provider="test",
            model="test-model",
            profile="fast",
            finish_reason="stop",
            native=True,
        )

    monkeypatch.setattr(
        "omnidesk_agent.models.router_streaming.stream_provider",
        invalid_json_stream,
    )
    from omnidesk_agent.models.router_streaming import GovernedStreamingRouter

    adapter = GovernedStreamingRouter(_StructuredRouter())  # type: ignore[arg-type]
    deltas = [
        delta
        async for delta in adapter.stream(
            ModelRequest(
                system="system",
                user="return json",
                task="chat",
                task_id="structured-1",
                json_mode=True,
                metadata={"actor": "operator-1"},
            )
        )
    ]

    visible_text = "".join(delta.text for delta in deltas if delta.text)
    assert visible_text == '{"ok":true}'
    assert "INVALID" not in visible_text
    assert any(delta.native is False for delta in deltas if delta.text)
