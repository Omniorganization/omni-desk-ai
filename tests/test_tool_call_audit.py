"""Tool calls that never reached a permission check must still leave an audit entry.

files.list ran with no permission check and no audit entry, and requests a tool
refused by its own policy (a path outside the workspace, a .git write, writes
disabled) raised before verify(), so nothing was audited. The runtime's own
SQLite stores live under the workspace root, where files.read_text/write_text
could read other runs' data or rewrite approvals.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from omnidesk_agent.config import PermissionConfig
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.files import FilesTool
from omnidesk_agent.tools.registry import ToolRegistry
from omnidesk_agent.tools.vision import VisionGroundingTool


class AuditingPermissions(PermissionManager):
    """The permission manager's audit API; the security session's own methods once they exist."""

    def record_access(self, proposal):
        base = getattr(super(), "record_access", None)
        if callable(base):
            return base(proposal)
        self._audit("access_without_approval", self._proposal_dict(proposal))

    def record_policy_block(self, proposal, *, rule, reason):
        base = getattr(super(), "record_policy_block", None)
        if callable(base):
            return base(proposal, rule=rule, reason=reason)
        self._audit("policy_blocked", {**self._proposal_dict(proposal), "block_rule": rule, "block_reason": reason})


@pytest.fixture
def setup(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "notes").mkdir(parents=True)
    (workspace / "notes" / "a.txt").write_text("a", encoding="utf-8")
    audit = tmp_path / "audit.jsonl"
    permissions = AuditingPermissions(PermissionConfig(default_mode="allow", audit_log=audit))
    tools = ToolRegistry()
    tools.register(FilesTool(workspace, allow_write=True))
    ctx = ToolContext(permissions=permissions, source="telegram", actor="alice", run_id="run-1", plan_id="plan-1", step_index=0)

    def call(tool: str, action: str, args: dict):
        return asyncio.run(tools.call(tool, action, args, ctx))

    def entries() -> list[dict]:
        return [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()] if audit.exists() else []

    return workspace, tools, call, entries, permissions


def test_files_list_is_audited(setup):
    workspace, _tools, call, entries, _ = setup
    assert call("files", "list", {"path": "notes"}).ok
    (entry,) = entries()
    assert entry["event"] == "access_without_approval"
    assert entry["payload"]["tool"] == "files" and entry["payload"]["action"] == "list"
    assert entry["payload"]["args"]["path"] == str((workspace / "notes").resolve())
    assert entry["payload"]["actor"] == "alice" and entry["payload"]["run_id"] == "run-1"


def test_a_listing_whose_audit_fails_does_not_run(setup, monkeypatch):
    _workspace, _tools, call, _entries, permissions = setup

    def broken(proposal):
        raise PermissionError("audit chain unusable")

    monkeypatch.setattr(permissions, "record_access", broken)
    result = call("files", "list", {"path": "notes"})
    assert result.ok is False and "audit chain unusable" in (result.error or "")
    assert result.data is None or "items" not in result.data


@pytest.mark.parametrize(
    ("action", "args", "rule"),
    [
        ("read_text", {"path": "../../etc/passwd"}, "path_escape"),
        ("write_text", {"path": ".git/config", "text": "[core]\n fsmonitor = evil"}, "git_metadata_write"),
        ("read_text", {"path": "runs.sqlite3"}, "runtime_state"),
        ("write_text", {"path": "approvals.sqlite3-wal", "text": "x"}, "runtime_state"),
        # A case-insensitive filesystem opens runs.sqlite3 for this name.
        ("read_text", {"path": "RUNS.SQLITE3"}, "runtime_state"),
        ("read_text", {"path": "audit_checkpoints/latest.json"}, "runtime_state"),
        ("write_text", {"path": "growth_plan.json", "text": "{}"}, "runtime_state"),
    ],
)
def test_policy_refusals_are_audited_with_their_rule(setup, action, args, rule):
    workspace, _tools, call, entries, _ = setup
    (workspace / "runs.sqlite3").write_bytes(b"SQLite format 3\x00")
    result = call("files", action, args)

    assert result.ok is False
    assert (result.error or "").startswith("PolicyRefusal: ")
    events = [e["event"] for e in entries()]
    assert events == ["policy_blocked"]  # refused before any proposal reached verify()
    payload = entries()[0]["payload"]
    assert payload["block_rule"] == rule and payload["tool"] == "files" and payload["action"] == action
    assert not (workspace / ".git").exists() and not (workspace / "approvals.sqlite3-wal").exists() and not (workspace / "growth_plan.json").exists()


def test_a_users_own_database_below_the_root_is_not_runtime_state(setup):
    # Only OmniDesk's entries in the workspace root are its state (sandbox.isolation.is_runtime_state).
    workspace, _tools, call, entries, _ = setup
    (workspace / "project").mkdir()
    (workspace / "project" / "runs.sqlite3").write_text("user data", encoding="utf-8")
    result = call("files", "read_text", {"path": "project/runs.sqlite3"})

    assert "PolicyRefusal" not in (result.error or "")
    assert entries()[0]["event"] == "proposal"  # it reached the permission check like any read


def test_writes_disabled_by_capability_are_audited(tmp_path):
    audit = tmp_path / "audit.jsonl"
    tools = ToolRegistry()
    tools.register(FilesTool(tmp_path / "ws"))
    ctx = ToolContext(permissions=AuditingPermissions(PermissionConfig(default_mode="allow", audit_log=audit)))
    assert asyncio.run(tools.call("files", "write_text", {"path": "x.txt", "text": "x"}, ctx)).ok is False
    (entry,) = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert entry["event"] == "policy_blocked" and entry["payload"]["block_rule"] == "capability_disabled"


def test_other_tools_refusals_are_audited_too(setup, tmp_path):
    workspace, tools, _call, entries, permissions = setup
    tools.register(VisionGroundingTool(router=None, workspace_root=workspace))  # type: ignore[arg-type]
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    ctx = ToolContext(permissions=permissions, source="telegram", actor="alice")
    result = asyncio.run(tools.call("vision", "ground", {"image_path": str(outside), "instruction": "x"}, ctx))

    assert result.ok is False
    (entry,) = entries()
    assert entry["event"] == "policy_blocked" and entry["payload"]["tool"] == "vision" and entry["payload"]["block_rule"] == "tool_policy"
    assert "escapes workspace" in entry["payload"]["block_reason"]


def test_a_permission_denial_is_audited_once_by_the_permission_manager(tmp_path):
    audit = tmp_path / "audit.jsonl"
    tools = ToolRegistry()
    tools.register(FilesTool(tmp_path / "ws", allow_write=True))
    # interactive_cli without a TTY denies every non-low step.
    ctx = ToolContext(permissions=AuditingPermissions(PermissionConfig(audit_log=audit)), source="telegram", actor="alice")
    assert asyncio.run(tools.call("files", "write_text", {"path": "x.txt", "text": "x"}, ctx)).ok is False
    events = [json.loads(line)["event"] for line in audit.read_text(encoding="utf-8").splitlines()]
    assert events == ["proposal", "denied_no_tty"]


def test_listing_still_shows_names_of_state_files(setup):
    workspace, _tools, call, _entries, _ = setup
    (workspace / "runs.sqlite3").write_bytes(b"x")
    listed = call("files", "list", {"path": "."})
    assert listed.ok and "runs.sqlite3" in {item["name"] for item in listed.data["items"]}
    assert Path(workspace / "runs.sqlite3").read_bytes() == b"x"
