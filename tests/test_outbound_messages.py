from __future__ import annotations

import asyncio
from pathlib import Path

from omnidesk_agent.core.outbound_dispatcher import OutboundDispatcher
from omnidesk_agent.core.outbound_messages import OutboundMessageStore
from omnidesk_agent.storage.sqlite import connect_sqlite
from omnidesk_agent.security.permissions import PermissionDecision
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.channel_send import ChannelSendTool


class AllowPermissions:
    def verify(self, proposal):
        return PermissionDecision(allowed=True, mode="allow", reason="ok")


class DummyAdapter:
    def __init__(self):
        self.sent = []

    async def send_text(self, recipient: str, text: str, **kwargs):
        self.sent.append((recipient, text, kwargs))
        return {"message_id": "provider-msg-1", "request_id": "provider-req-1"}


class FailingAdapter:
    async def send_text(self, recipient: str, text: str, **kwargs):
        raise RuntimeError("provider down")


def test_outbound_message_store_lifecycle(tmp_path: Path):
    store = OutboundMessageStore(tmp_path / "outbound.sqlite3")
    msg_id = store.create(channel="telegram", recipient="u1", payload={"type": "text", "text": "hello"})

    assert store.list(status="pending")[0]["id"] == msg_id

    claimed = store.claim_next()
    assert claimed and claimed["id"] == msg_id
    assert store.get(msg_id)["status"] == "running"

    store.mark_sent(msg_id, provider_message_id="m1", provider_request_id="r1")
    sent = store.list(status="sent")[0]
    assert sent["provider_message_id"] == "m1"
    assert sent["provider_request_id"] == "r1"

    failed_id = store.create(channel="line", recipient="u2", payload={"type": "text", "text": "bad"}, max_retries=0)
    claim = store.claim_next()
    assert claim and claim["id"] == failed_id
    result = store.mark_failed(failed_id, "boom")
    assert result["status"] == "dead_letter"
    dead = store.list(status="dead_letter")[0]
    assert dead["last_error"] == "boom"
    assert dead["retry_count"] == 1

    assert store.requeue(failed_id)["status"] == "pending"
    assert store.cancel(failed_id)["status"] == "cancelled"


def test_outbound_idempotency_key_deduplicates_creates(tmp_path: Path):
    store = OutboundMessageStore(tmp_path / "outbound.sqlite3")
    first = store.create(channel="telegram", recipient="u1", payload={"type": "text", "text": "hello"}, idempotency_key="stable-key")
    second = store.create(channel="telegram", recipient="u1", payload={"type": "text", "text": "hello again"}, idempotency_key="stable-key")

    assert first == second
    row = store.find_by_idempotency_key("stable-key")
    assert row is not None
    assert row["id"] == first
    assert row["idempotency_key"] == "stable-key"


def test_channel_send_queues_outbound_pending(tmp_path: Path):
    store = OutboundMessageStore(tmp_path / "outbound.sqlite3")
    adapter = DummyAdapter()
    tool = ChannelSendTool({"telegram": adapter}, store)
    ctx = ToolContext(permissions=AllowPermissions(), source="test", actor="owner")

    result = asyncio.run(tool.call("send_text", {"channel": "telegram", "recipient": "u1", "text": "hello", "options": {"silent": True}}, ctx))

    assert result.ok is True
    assert result.data["status"] == "pending"
    row = store.list(status="pending")[0]
    assert row["id"] == result.data["outbound_id"]
    assert adapter.sent == []


def test_outbound_dispatcher_marks_sent(tmp_path: Path):
    async def run_case():
        store = OutboundMessageStore(tmp_path / "outbound.sqlite3")
        adapter = DummyAdapter()
        outbound_id = store.create(channel="telegram", recipient="u1", payload={"type": "text", "text": "hello", "options": {"silent": True}})
        dispatcher = OutboundDispatcher(store, {"telegram": adapter})

        assert await dispatcher.run_once() is True

        row = store.get(outbound_id)
        assert row["status"] == "sent"
        assert row["provider_message_id"] == "provider-msg-1"
        assert row["provider_request_id"] == "provider-req-1"
        assert adapter.sent[0][0:2] == ("u1", "hello")
        assert adapter.sent[0][2]["silent"] is True
        assert adapter.sent[0][2]["idempotency_key"] == row["idempotency_key"]

    asyncio.run(run_case())


def test_outbound_dispatcher_retries_then_dead_letters(tmp_path: Path):
    async def run_case():
        store = OutboundMessageStore(tmp_path / "outbound.sqlite3", max_retries=1, base_retry_seconds=1)
        outbound_id = store.create(channel="telegram", recipient="u1", payload={"type": "text", "text": "hello"}, max_retries=1)
        dispatcher = OutboundDispatcher(store, {"telegram": FailingAdapter()})

        assert await dispatcher.run_once() is True
        first = store.get(outbound_id)
        assert first["status"] == "retry"
        assert first["retry_count"] == 1

        # Force retry to be due immediately.
        with connect_sqlite(store.db_path) as con:
            con.execute("UPDATE outbound_messages SET next_retry_at=0 WHERE id=?", (outbound_id,))

        assert await dispatcher.run_once() is True
        second = store.get(outbound_id)
        assert second["status"] == "dead_letter"
        assert second["retry_count"] == 2
        assert "provider down" in second["last_error"]

    asyncio.run(run_case())


def test_outbound_stale_running_recovery(tmp_path: Path):
    store = OutboundMessageStore(tmp_path / "outbound.sqlite3", base_retry_seconds=1)
    outbound_id = store.create(channel="telegram", recipient="u1", payload={"type": "text", "text": "hello"})
    assert store.claim_next()["id"] == outbound_id

    with connect_sqlite(store.db_path) as con:
        con.execute("UPDATE outbound_messages SET locked_at=1 WHERE id=?", (outbound_id,))

    assert store.recover_stale_running(lease_seconds=300) == 1
    row = store.get(outbound_id)
    assert row["status"] == "retry"
    assert row["retry_count"] == 1

class TimeoutAdapter:
    async def send_text(self, recipient: str, text: str, **kwargs):
        raise TimeoutError("provider timeout after request write")


def test_outbound_dispatcher_marks_best_effort_timeout_as_ambiguous(tmp_path: Path):
    async def run_case():
        store = OutboundMessageStore(tmp_path / "outbound.sqlite3", max_retries=3, base_retry_seconds=1)
        outbound_id = store.create(channel="telegram", recipient="u1", payload={"type": "text", "text": "hello"}, idempotency_key="idem-1")
        dispatcher = OutboundDispatcher(store, {"telegram": TimeoutAdapter()})

        assert await dispatcher.run_once() is True
        row = store.get(outbound_id)
        assert row["status"] == "ambiguous"
        assert row["error_category"] == "timeout"
        assert row["provider_request_id"] == "idem-1"
        assert store.list_ambiguous()[0]["id"] == outbound_id

    asyncio.run(run_case())
