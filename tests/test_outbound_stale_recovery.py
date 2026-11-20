"""A send that was interrupted must not be sent again by itself.

When the dispatcher dies between claiming a message and recording the result,
the provider may already have delivered it. recover_stale_running used to put
every such message back in the retry queue, so a crash or restart could send a
customer the same message twice. Only providers that deduplicate by our
idempotency key ('native') make a second send harmless; no channel has that
today, so interrupted sends now wait in 'ambiguous' for reconciliation.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnidesk_agent.channels import idempotency
from omnidesk_agent.channels.idempotency import ProviderIdempotencyCapability
from omnidesk_agent.core.outbound_dispatcher import OutboundDispatcher
from omnidesk_agent.core.outbound_messages import OutboundMessageStore
from omnidesk_agent.repositories.postgres_state import PostgresOutboundMessageStore
from omnidesk_agent.storage.sqlite import connect_sqlite
from test_postgres_state_runtime_contracts import MemoryJsonState


class CountingAdapter:
    def __init__(self, error: BaseException | None = None):
        self.sends: list[str] = []
        self.error = error

    async def send_text(self, recipient: str, text: str, **kwargs):
        self.sends.append(text)
        if self.error is not None:
            raise self.error
        return {"message_id": f"m{len(self.sends)}"}


def _sqlite_store(tmp_path: Path) -> OutboundMessageStore:
    return OutboundMessageStore(tmp_path / "outbound.sqlite3", max_retries=2, base_retry_seconds=1)


def _crash_mid_send(store, message_id: str) -> None:
    """The dispatcher claimed the message and died before recording a result."""
    claimed = store.claim_next()
    assert claimed and claimed["id"] == message_id
    if isinstance(store, OutboundMessageStore):
        with connect_sqlite(store.db_path) as con:
            con.execute("UPDATE outbound_messages SET locked_at=1 WHERE id=?", (message_id,))
    else:
        store.state.rows[store.namespace][store.get(message_id)["idempotency_key"]]["locked_at"] = 1


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "sqlite":
        return _sqlite_store(tmp_path)
    return PostgresOutboundMessageStore(MemoryJsonState(), max_retries=2, base_retry_seconds=1)


def test_an_interrupted_best_effort_send_waits_for_reconciliation(store):
    message_id = store.create(channel="telegram", recipient="u1", payload={"type": "text", "text": "invoice ready"}, idempotency_key="idem-1")
    _crash_mid_send(store, message_id)

    assert store.recover_stale_running(lease_seconds=300) == 1
    row = store.get(message_id)
    assert row["status"] == "ambiguous" and row["error_category"] == "stale_running"
    assert store.claim_next() is None  # never re-sent by the dispatcher on its own
    assert [r["id"] for r in store.list_ambiguous()] == [message_id]
    # After checking the provider, an operator can still send it again on purpose.
    assert store.requeue(message_id)["status"] == "pending"


def test_an_interrupted_send_to_a_deduplicating_provider_is_retried(store, monkeypatch):
    monkeypatch.setitem(idempotency.PROVIDER_IDEMPOTENCY, "dedupe_chat", ProviderIdempotencyCapability("dedupe_chat", "native", "Idempotency-Key", duplicate_risk=False))
    message_id = store.create(channel="dedupe_chat", recipient="u1", payload={"type": "text", "text": "hi"}, idempotency_key="idem-2")
    _crash_mid_send(store, message_id)

    assert store.recover_stale_running(lease_seconds=300) == 1
    assert store.get(message_id)["status"] == "retry"


def test_dispatcher_restart_does_not_send_an_interrupted_message_twice(tmp_path):
    async def scenario():
        store = _sqlite_store(tmp_path)
        message_id = store.create(channel="telegram", recipient="u1", payload={"type": "text", "text": "pay now"}, idempotency_key="idem-3")
        _crash_mid_send(store, message_id)  # first process: claimed, maybe delivered, then killed
        adapter = CountingAdapter()
        dispatcher = OutboundDispatcher(store, {"telegram": adapter}, lease_seconds=300)
        dispatcher.start()  # second process: recovers stale rows, then polls
        await asyncio.sleep(0.05)
        await dispatcher.stop()
        return store.get(message_id), adapter.sends

    row, sends = asyncio.run(scenario())
    assert row["status"] == "ambiguous" and sends == []


class ChannelError(RuntimeError):
    """Shape of the channels session's ChannelHttpError with request_sent."""

    def __init__(self, message: str, *, request_sent):
        super().__init__(message)
        self.request_sent = request_sent


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ChannelError("network error: connection refused", request_sent=False), "retry"),
        (ChannelError("network error: connection reset by peer", request_sent=True), "ambiguous"),
        (ChannelError("request timed out", request_sent=None), "ambiguous"),
        (TimeoutError("provider timeout after request write"), "ambiguous"),
    ],
)
def test_dispatcher_retries_only_what_never_left(tmp_path, error, status):
    async def scenario():
        store = _sqlite_store(tmp_path)
        message_id = store.create(channel="telegram", recipient="u1", payload={"type": "text", "text": "hello"})
        await OutboundDispatcher(store, {"telegram": CountingAdapter(error)}).run_once()
        return store.get(message_id)

    assert asyncio.run(scenario())["status"] == status
