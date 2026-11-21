"""The per-call execution wrapper hands everything but verify to the real permission manager.

permission_guarded wraps ctx.permissions for each tool call, so that a denial
the manager returns stops the operation. The wrapper subclasses
PermissionManager only for its type. If a PermissionManager method ran with
the wrapper as self, the state it writes would land on the throwaway wrapper:
an audit-integrity failure flag, say, or a derived key. The real manager
would then keep accepting decisions. These methods are added to
PermissionManager itself here, as security's own are.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac

import pytest

from omnidesk_agent.config import PermissionConfig
from omnidesk_agent.core.models import ApprovalDecision, ToolResult
from omnidesk_agent.security.approval_store import ApprovalStore
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.tools.base import ToolContext, permission_guarded, proposal
from omnidesk_agent.tools.files import FilesTool


class Manager(PermissionManager):
    """Allows everything until it is marked failed; can also deny."""

    deny = False

    def verify(self, action_proposal):
        if getattr(self, "failure", None):
            raise PermissionError(f"refused: {self.failure}")
        if self.deny:
            return ApprovalDecision(False, "deny", "no")
        return ApprovalDecision(True, "allow", "test")


class MarksFailure:
    """A tool that reports an audit failure through the context it was handed."""

    name = "probe"

    @permission_guarded
    async def call(self, action, args, ctx):
        ctx.permissions.mark_failure("audit log unavailable")
        return ToolResult(True, summary="marked")


def manager(tmp_path) -> Manager:
    return Manager(PermissionConfig(audit_log=tmp_path / "audit.jsonl"), ApprovalStore(tmp_path / "approvals.sqlite3"))


def test_a_failure_marked_through_the_wrapper_makes_the_manager_refuse(tmp_path, monkeypatch):
    # Like security's mark_audit_integrity_failure: a PermissionManager method that writes self-state.
    monkeypatch.setattr(PermissionManager, "mark_failure", lambda self, reason: setattr(self, "failure", reason), raising=False)
    real = manager(tmp_path)

    assert asyncio.run(MarksFailure().call("mark", {}, ToolContext(permissions=real))).ok

    # Fail closed: the real manager, not the throwaway wrapper, now refuses.
    with pytest.raises(PermissionError, match="refused"):
        real.verify(proposal("files", "read_text", {"path": "x"}, "medium", "test", ToolContext(permissions=real)))


def test_a_key_derived_through_the_wrapper_is_derived_once(tmp_path, monkeypatch):
    derivations: list[int] = []

    def keyed_digest(self, data: bytes) -> str:
        if getattr(self, "_test_key", None) is None:
            derivations.append(1)
            self._test_key = b"derived-sub-key"
        return hmac.new(self._test_key, data, hashlib.sha256).hexdigest()

    monkeypatch.setattr(PermissionManager, "keyed_digest", keyed_digest, raising=False)
    real = manager(tmp_path)
    files = FilesTool(tmp_path / "ws", allow_write=True)

    for name in ("a.txt", "b.txt"):  # two calls, two wrappers
        assert asyncio.run(files.call("write_text", {"path": name, "text": "x"}, ToolContext(permissions=real))).ok

    assert derivations == [1]  # cached on the manager, not on each call's wrapper


def test_verify_is_still_the_wrappers_own_so_a_denial_stops_the_call(tmp_path):
    real = manager(tmp_path)
    real.deny = True
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "notes.txt").write_text("secret", encoding="utf-8")

    # The tool raises; through the registry the step becomes a failed result.
    with pytest.raises(PermissionError, match="did not authorize"):
        asyncio.run(FilesTool(tmp_path / "ws").call("read_text", {"path": "notes.txt"}, ToolContext(permissions=real)))
