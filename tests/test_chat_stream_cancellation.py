"""A cancelled chat stream always ends with exactly one final event.

#237 moved the stream's store writes onto the worker pool. Cancelling an await
does not stop a write already running there (Codex, on #237), so a client that
disconnects mid-write must not get an interruption marker under the sequence of
the event being written: on PostgreSQL that marker is refused as a sequence
conflict, and in the JSON store it is silently dropped. Either way the request
was left streaming, with no final event, until its lease expired. Once the model
has answered, a disconnect must not stop the turn from being stored and completed.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable

import pytest

from omnidesk_agent.appsync.chat_service import ChatTurnService
from omnidesk_agent.appsync.store import AppSyncStore
from omnidesk_agent.models.router import RoutePlan

ACTOR = "operator-1"


class _Router:
    def route_plan(self, _task: str, _metadata: dict[str, object]) -> RoutePlan:
        return RoutePlan(profiles=["fast"], max_retries=0)


class _Connected:
    async def is_disconnected(self) -> bool:
        return False


def _delta(text: str, finish_reason: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(provider="test", model="test-model", profile="fast", native=True, provider_request_id=None,
                           text=text, reasoning="", usage={"output_tokens": 1} if finish_reason else None,
                           finish_reason=finish_reason)


class _Stream:
    def __init__(self, *, finishes: bool) -> None:
        self.finishes = finishes

    async def stream(self, _request: object) -> AsyncIterator[SimpleNamespace]:
        if self.finishes:
            yield _delta("Hello", finish_reason="stop")
            return
        yield _delta("Hel")
        await asyncio.Event().wait()  # the model is still answering when the client leaves


def _service(tmp_path: Path, key: str, *, finishes: bool) -> tuple[ChatTurnService, Any]:
    cfg = SimpleNamespace(app_sync=SimpleNamespace(require_idempotency=True),
                          api_resource_guard=SimpleNamespace(max_inflight_chat_requests=2))
    service = ChatTurnService(cfg=cfg, runtime=SimpleNamespace(model_router=_Router()), store=AppSyncStore(tmp_path / "appsync.json"))
    conversation_id = service.store.create_conversation(actor=ACTOR, title="Cancelled stream")["conversation_id"]
    request = SimpleNamespace(headers={"idempotency-key": key}, state=SimpleNamespace(), client=None)
    prepared = service.prepare_stream(request=request, payload={"conversation_id": conversation_id, "content": "hello"},  # type: ignore[arg-type]
                                      actor=ACTOR, role="operator")
    service.streaming_router = _Stream(finishes=finishes)  # type: ignore[assignment]
    return service, prepared


def _hold(service: ChatTurnService, name: str, when: Callable[[dict[str, Any]], bool]) -> tuple[threading.Event, threading.Event, threading.Event]:
    """Hold the first matching store call on the worker pool until released."""
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    original = getattr(service, name)

    def held(**kwargs: Any) -> Any:
        if entered.is_set() or not when(kwargs):
            return original(**kwargs)
        entered.set()
        assert release.wait(5)
        try:
            return original(**kwargs)
        finally:
            done.set()

    setattr(service, name, held)
    return entered, release, done


async def _cancel_while_held(service: ChatTurnService, prepared: Any, entered: threading.Event, release: threading.Event) -> None:
    async def consume() -> None:
        async for _ in service.stream_prepared(request=_Connected(), prepared=prepared, actor=ACTOR):  # type: ignore[arg-type]
            pass

    task = asyncio.create_task(consume())
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()  # the client disconnects while the store call runs
    await asyncio.sleep(0.05)  # the old code wrote its marker here, under the held event's sequence
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


def _stored(service: ChatTurnService, prepared: Any) -> tuple[list[tuple[int, str]], str]:
    cached = service.store.get_idempotency_response(actor=ACTOR, endpoint="conversations.ask", key=prepared.idempotency_key,
                                                    payload=prepared.idempotency_payload) or {}
    return [(int(item["sequence"]), str(item["event"])) for item in cached.get("stream_events") or []], str(cached.get("stream_status"))


@pytest.mark.asyncio
async def test_a_disconnect_during_a_delta_write_puts_the_marker_after_that_delta(tmp_path: Path) -> None:
    service, prepared = _service(tmp_path, "cancel-delta", finishes=False)
    entered, release, done = _hold(service, "_append_stream_event", lambda call: call["event"].event == "chat.delta")
    await _cancel_while_held(service, prepared, entered, release)
    assert await asyncio.to_thread(done.wait, 5)
    assert _stored(service, prepared) == ([(1, "chat.started"), (2, "chat.delta"), (3, "chat.failed")], "interrupted")


@pytest.mark.asyncio
async def test_a_disconnect_during_the_started_write_is_still_recorded(tmp_path: Path) -> None:
    service, prepared = _service(tmp_path, "cancel-started", finishes=False)
    entered, release, done = _hold(service, "_append_stream_event", lambda call: call["event"].event == "chat.started")
    await _cancel_while_held(service, prepared, entered, release)
    assert await asyncio.to_thread(done.wait, 5)
    assert _stored(service, prepared) == ([(1, "chat.started"), (2, "chat.failed")], "interrupted")


@pytest.mark.asyncio
async def test_a_disconnect_after_the_answer_still_stores_and_completes_the_turn(tmp_path: Path) -> None:
    service, prepared = _service(tmp_path, "cancel-finish", finishes=True)
    entered, release, _ = _hold(service, "_persist_result", lambda _call: True)
    _, never, completed = _hold(service, "_append_stream_event", lambda call: call["event"].event == "chat.completed")
    never.set()  # only used to see the completed event being written
    await _cancel_while_held(service, prepared, entered, release)
    assert await asyncio.to_thread(completed.wait, 5)
    await asyncio.sleep(0)
    assert _stored(service, prepared) == ([(1, "chat.started"), (2, "chat.delta"), (3, "chat.usage"), (4, "chat.completed")], "completed")
    messages = service.store.list_messages(prepared.conversation_id, actor=ACTOR)
    assert [message["content"] for message in messages if message.get("role") == "assistant"] == ["Hello"]
