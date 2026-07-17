from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from omnidesk_agent.appsync.industrial_chat_service import IndustrialChatTurnService
from omnidesk_agent.appsync.lease_safe_chat_repository import ChatLeaseLost, ChatReservation


def _reservation() -> ChatReservation:
    return ChatReservation(
        namespace="test",
        organization_id="org",
        actor="operator",
        endpoint="conversations.ask",
        idempotency_key="heartbeat-key",
        payload_hash="hash",
        conversation_id="conv",
        user_message={"message_id": "msg"},
        status="running",
        lease_owner="worker",
        response={},
        events=(),
    )


@pytest.mark.asyncio
async def test_heartbeat_retries_transient_database_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class Repository:
        lease_seconds = 30

        def __init__(self) -> None:
            self.calls = 0

        def renew_lease(self, reservation: ChatReservation) -> None:
            del reservation
            self.calls += 1
            if self.calls == 1:
                raise OSError("temporary database outage")
            raise ChatLeaseLost("lease was reclaimed")

    repository = Repository()
    service = cast(Any, object.__new__(IndustrialChatTurnService))
    service.atomic_repository = repository

    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    await service._lease_heartbeat(_reservation())
    assert repository.calls == 2


@pytest.mark.asyncio
async def test_cancel_heartbeat_does_not_mask_success_with_task_failure() -> None:
    service = cast(Any, object.__new__(IndustrialChatTurnService))

    async def failed() -> None:
        raise OSError("heartbeat failed before cleanup")

    task: asyncio.Task[Any] = asyncio.create_task(failed())
    await asyncio.sleep(0)
    await service._cancel_heartbeat(task)
