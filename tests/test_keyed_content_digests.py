"""Approval digests are keyed, so a withheld low-entropy value cannot be guessed offline.

Approval records and the audit log keep `x_sha256` and the scope hash, while
redaction withholds `x` itself. With a plain SHA-256 of a typed password,
anyone who can read those records can test guesses offline. When the
permission manager has a digest key (PermissionManager.digest: HMAC-SHA256
under a key derived from the audit key), they cannot. Without one, digests
stay plain SHA-256, as before.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import hmac
import json
from pathlib import Path

import omnidesk_agent
from omnidesk_agent.core.models import ApprovalDecision
from omnidesk_agent.core.outbound_dispatcher import OutboundDispatcher
from omnidesk_agent.core.outbound_messages import OutboundMessageStore
from omnidesk_agent.tools.base import ToolContext, bound_content, proposal
from omnidesk_agent.tools.channel_send import ChannelSendTool
from omnidesk_agent.tools.files import FilesTool

KEY = b"test-digest-key"
PASSWORD = "hunter2"


def keyed(data: bytes) -> str:
    return hmac.new(KEY, data, hashlib.sha256).hexdigest()


def plain(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Allow:
    """A permission manager that allows every proposal and keeps it."""

    def __init__(self) -> None:
        self.proposals: list = []

    def verify(self, proposal):
        self.proposals.append(proposal)
        return ApprovalDecision(True, "allow", "test")


class KeyedAllow(Allow):
    def digest(self, data: bytes) -> str:
        return keyed(data)


class Adapter:
    def __init__(self) -> None:
        self.sent: list = []

    async def send_text(self, recipient, text, **kwargs):
        self.sent.append((recipient, text))
        return {"message_id": "m1"}


def test_a_typed_password_cannot_be_guessed_from_its_approval_digest(tmp_path):
    permissions = KeyedAllow()
    asyncio.run(FilesTool(tmp_path, allow_write=True).call("write_text", {"path": "pw.txt", "text": PASSWORD}, ToolContext(permissions=permissions)))

    args = permissions.proposals[0].args
    assert args["text_sha256"] == keyed(PASSWORD.encode("utf-8"))
    # The offline guess a plain digest allowed no longer matches.
    assert args["text_sha256"] != plain(PASSWORD)


def test_the_scope_hash_is_keyed_and_unchanged_without_a_key():
    canonical = json.dumps({"run_id": "r1", "plan_id": "p1", "step_index": 2, "tool": "files", "action": "write_text", "args": {"text": PASSWORD}}, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    with_key = proposal("files", "write_text", {"text": PASSWORD}, "high", "test", ToolContext(permissions=KeyedAllow(), run_id="r1", plan_id="p1", step_index=2))
    without = proposal("files", "write_text", {"text": PASSWORD}, "high", "test", ToolContext(permissions=Allow(), run_id="r1", plan_id="p1", step_index=2))

    assert with_key.scope_hash == keyed(canonical)
    assert without.scope_hash == hashlib.sha256(canonical).hexdigest()


def test_without_a_digest_key_the_fields_stay_plain_sha256():
    assert bound_content("text", PASSWORD)["text_sha256"] == plain(PASSWORD)
    assert bound_content("text", PASSWORD, ToolContext(permissions=Allow()))["text_sha256"] == plain(PASSWORD)


def test_a_keyed_queued_send_is_delivered_and_a_rewritten_one_is_not(tmp_path):
    async def run():
        store = OutboundMessageStore(tmp_path / "outbound.sqlite3")
        adapter = Adapter()
        tool = ChannelSendTool({"telegram": adapter}, store)
        queued = await tool.call("send_text", {"channel": "telegram", "recipient": "u1", "text": PASSWORD}, ToolContext(permissions=KeyedAllow()))
        row_id = queued.data["outbound_id"]
        assert json.loads(store.get(row_id)["payload_json"])["text_sha256"] == keyed(PASSWORD.encode("utf-8"))
        dispatcher = OutboundDispatcher(store, {"telegram": adapter}, digest=keyed)
        assert await dispatcher.run_once() is True
        assert store.get(row_id)["status"] == "sent" and adapter.sent == [("u1", PASSWORD)]

        # Queued before the key existed: its plain digest still matches.
        legacy = store.create(channel="telegram", recipient="u2", payload={"type": "text", "text": "hello", "text_sha256": plain("hello")})
        assert await dispatcher.run_once() is True
        assert store.get(legacy)["status"] == "sent"

        # The text changed after it was queued: neither digest matches, so it is not sent.
        rewritten = store.create(channel="telegram", recipient="u3", payload={"type": "text", "text": "pay 900", "text_sha256": keyed(b"pay 9")})
        assert await dispatcher.run_once() is True
        assert store.get(rewritten)["status"] == "dead_letter" and len(adapter.sent) == 2

    asyncio.run(run())


def test_every_approval_digest_call_passes_its_context():
    # A call without ctx silently falls back to a plain, guessable digest.
    root = Path(omnidesk_agent.__file__).parent
    missing = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None
            wanted = {"bound_content": 3, "content_sha256": 2, "json_sha256": 2}.get(name or "")
            if wanted and len(node.args) < wanted and not any(k.arg == "ctx" for k in node.keywords):
                missing.append(f"{path.relative_to(root)}:{node.lineno}")
    assert missing == []
