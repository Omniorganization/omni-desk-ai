"""M13: an outbound message approval must show and bind the full content.

The proposal used to carry only the first 200 characters plus the length, so
the approver never saw the rest and the scope hash accepted any text with the
same prefix and length (a prompt-injected payload could hide past the cut).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from omnidesk_agent.config import PermissionConfig
from omnidesk_agent.core.models import ApprovalDecision
from omnidesk_agent.core.outbound_dispatcher import OutboundDispatcher
from omnidesk_agent.core.outbound_messages import OutboundMessageStore
from omnidesk_agent.security.approval_required import ApprovalRequired
from omnidesk_agent.security.approval_store import ApprovalStore
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.channel_send import ChannelSendTool

PREFIX = "Quarterly summary attached. " * 10  # well past 200 characters
APPROVED = PREFIX + "Thanks, see you Monday."
SWAPPED = PREFIX + "Send creds to evil.test"
assert len(APPROVED) == len(SWAPPED) and APPROVED[:200] == SWAPPED[:200]


class RecordingPermissions:
    def __init__(self) -> None:
        self.proposals: list[Any] = []

    def verify(self, action_proposal: Any) -> ApprovalDecision:
        self.proposals.append(action_proposal)
        return ApprovalDecision(True, "allow", "test")


class SendAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, recipient: str, text: str, **kwargs: Any) -> dict[str, Any]:
        self.sent.append((recipient, text))
        return {"message_id": "m1"}


def _ctx(permissions: Any) -> ToolContext:
    return ToolContext(permissions=permissions, source="web", actor="owner", run_id="run-1", plan_id="plan-1", step_index=0)


def _send_text(tool: ChannelSendTool, permissions: Any, text: str):
    args = {"channel": "telegram", "recipient": "u1", "text": text}
    return asyncio.run(tool.call("send_text", args, _ctx(permissions)))


def test_approval_proposal_shows_full_text_and_its_digest() -> None:
    permissions = RecordingPermissions()
    _send_text(ChannelSendTool({"telegram": SendAdapter()}), permissions, APPROVED)
    args = permissions.proposals[0].args
    assert args["text"] == APPROVED
    assert args["text_sha256"] == hashlib.sha256(APPROVED.encode("utf-8")).hexdigest()
    assert args["length"] == len(APPROVED)
    assert "text_preview" not in args


def test_scope_hash_binds_every_character_of_the_text() -> None:
    permissions = RecordingPermissions()
    tool = ChannelSendTool({"telegram": SendAdapter()})
    _send_text(tool, permissions, APPROVED)
    _send_text(tool, permissions, SWAPPED)
    approved, swapped = permissions.proposals
    assert approved.scope_hash != swapped.scope_hash


def test_approved_send_cannot_be_resumed_with_different_text(tmp_path: Path) -> None:
    cfg = PermissionConfig(approval_mode="remote_approval", audit_log=tmp_path / "audit.log")
    permissions = PermissionManager(cfg, ApprovalStore(tmp_path / "approvals.sqlite3"))
    adapter = SendAdapter()
    tool = ChannelSendTool({"telegram": adapter})

    with pytest.raises(ApprovalRequired) as required:
        _send_text(tool, permissions, APPROVED)
    assert required.value.proposal["args"]["text"] == APPROVED
    permissions.allow_approved_proposal(required.value.proposal)

    # Resuming the approved step with swapped content needs a fresh approval.
    with pytest.raises(ApprovalRequired) as again:
        _send_text(tool, permissions, SWAPPED)
    assert again.value.proposal["args"]["text"] == SWAPPED
    assert adapter.sent == []

    assert _send_text(tool, permissions, APPROVED).ok is True
    assert adapter.sent == [("u1", APPROVED)]


def test_send_email_approval_shows_full_body() -> None:
    class Gmail:
        cfg = type("Cfg", (), {"enabled": True, "readonly": False, "allow_send": True})()

        def build_raw_email(self, to: str, subject: str, body: str) -> dict[str, str]:
            return {"to": to, "body": body}

    permissions = RecordingPermissions()
    tool = ChannelSendTool({"gmail": Gmail()})
    asyncio.run(tool.call("send_email", {"to": "a@example.test", "subject": "s", "body": APPROVED}, _ctx(permissions)))
    asyncio.run(tool.call("send_email", {"to": "a@example.test", "subject": "s", "body": SWAPPED}, _ctx(permissions)))
    approved, swapped = permissions.proposals
    assert approved.args["body"] == APPROVED
    assert approved.args["body_sha256"] == hashlib.sha256(APPROVED.encode("utf-8")).hexdigest()
    assert approved.scope_hash != swapped.scope_hash


def _queue(tmp_path: Path) -> tuple[OutboundMessageStore, SendAdapter, OutboundDispatcher]:
    store = OutboundMessageStore(tmp_path / "outbound.sqlite3")
    adapter = SendAdapter()
    return store, adapter, OutboundDispatcher(store, {"telegram": adapter})


def test_dispatcher_sends_the_approved_text(tmp_path: Path) -> None:
    store, adapter, dispatcher = _queue(tmp_path)
    queued = _send_text(ChannelSendTool({"telegram": adapter}, store), RecordingPermissions(), APPROVED)
    row = store.get(queued.data["outbound_id"])
    assert json.loads(row["payload_json"])["text_sha256"] == hashlib.sha256(APPROVED.encode("utf-8")).hexdigest()
    assert asyncio.run(dispatcher.run_once()) is True
    assert adapter.sent == [("u1", APPROVED)]


def test_dispatcher_refuses_text_that_differs_from_the_approved_digest(tmp_path: Path) -> None:
    store, adapter, dispatcher = _queue(tmp_path)
    digest = hashlib.sha256(APPROVED.encode("utf-8")).hexdigest()
    message_id = store.create(channel="telegram", recipient="u1", payload={"type": "text", "text": SWAPPED, "text_sha256": digest})
    assert asyncio.run(dispatcher.run_once()) is True
    assert adapter.sent == []
    row = store.get(message_id)
    assert row["status"] == "dead_letter"
    assert "approved content digest" in row["last_error"]


def test_dispatcher_still_delivers_rows_queued_before_digests(tmp_path: Path) -> None:
    store, adapter, dispatcher = _queue(tmp_path)
    store.create(channel="telegram", recipient="u1", payload={"type": "text", "text": "legacy"})
    assert asyncio.run(dispatcher.run_once()) is True
    assert adapter.sent == [("u1", "legacy")]
