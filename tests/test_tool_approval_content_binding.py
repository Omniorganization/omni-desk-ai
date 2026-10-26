"""R-M1: every side-effecting tool's approval must show and bind the full content.

Several tools put only the first 200/300 characters (or nothing but a length)
into the proposal, so the approver never saw the rest and the scope hash
accepted any content with the same prefix and length. The M13 fix covered
channels.send_text only; these tests cover the other tools and scan every
proposal for a truncating slice or a length without the content.

Execution is bound through the scope hash: a resumed or session-approved call
recomputes it from the content it is about to act on (see the resume tests).
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from omnidesk_agent.config import ChromeConfig, PermissionConfig, UIBridgeConfig
from omnidesk_agent.core.models import ApprovalDecision
from omnidesk_agent.models.base import ModelResponse
from omnidesk_agent.plugins.subprocess_runner import SubprocessPluginTool
from omnidesk_agent.security.approval_required import ApprovalRequired
from omnidesk_agent.security.approval_store import ApprovalStore
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.tools.base import ToolContext, bound_content, content_sha256, json_sha256
from omnidesk_agent.tools.browser import BrowserTool
from omnidesk_agent.tools.computer import ComputerTool
from omnidesk_agent.tools.files import FilesTool
from omnidesk_agent.tools.gmail_tool import GmailTool
from omnidesk_agent.tools.pr_tool import PullRequestTool
from omnidesk_agent.tools.ui_bridge_tool import UIBridgeTool
from omnidesk_agent.tools.vision import VisionGroundingTool

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "Routine status update for the team. " * 16  # past every old 200/300/500 cut
APPROVED = PREFIX + "Thanks, see you Monday."
SWAPPED = PREFIX + "Send creds to evil.test"
assert len(APPROVED) == len(SWAPPED) and len(PREFIX) > 500
DIGEST = hashlib.sha256(APPROVED.encode("utf-8")).hexdigest()
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


class RecordingPermissions:
    def __init__(self) -> None:
        self.proposals: list[Any] = []

    def verify(self, action_proposal: Any) -> ApprovalDecision:
        self.proposals.append(action_proposal)
        return ApprovalDecision(True, "allow", "test")


def _ctx(permissions: Any) -> ToolContext:
    return ToolContext(permissions=permissions, source="web", actor="owner", run_id="run-1", plan_id="plan-1", step_index=0)


def _run(tool: Any, action: str, args: dict[str, Any], permissions: Any):
    return asyncio.run(tool.call(action, args, _ctx(permissions)))


# --- static scan -----------------------------------------------------------

def _is_proposal_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
    return name in {"proposal", "ActionProposal"}


def _is_len_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len"


def _truncations(source: str, label: str) -> list[str]:
    """In proposal(...) calls: slices, and len(x) without x itself; *_preview keys in modules that build proposals."""
    found: list[str] = []
    nodes = list(ast.walk(ast.parse(source, filename=label)))
    builds_proposals = any(_is_proposal_call(node) for node in nodes)
    for node in nodes:
        if _is_proposal_call(node):
            inner_nodes = list(ast.walk(node))
            for inner in inner_nodes:
                if isinstance(inner, ast.Subscript) and isinstance(inner.slice, ast.Slice):
                    found.append(f"{label}:{inner.lineno}: slice inside a proposal: {ast.unparse(inner)}")
            measured = {arg.id for call in inner_nodes if _is_len_call(call) for arg in call.args if isinstance(arg, ast.Name)}
            inside_len = {id(arg) for call in inner_nodes if _is_len_call(call) for arg in call.args}
            shown = {n.id for n in inner_nodes if isinstance(n, ast.Name) and id(n) not in inside_len}
            for name in sorted(measured - shown):
                found.append(f"{label}:{node.lineno}: proposal carries len({name}) but not {name}")
        if builds_proposals and isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value.endswith("_preview"):
                    found.append(f"{label}:{key.lineno}: preview field {key.value!r}")
    return found


def test_no_proposal_truncates_or_previews_its_arguments():
    sources = sorted((ROOT / "omnidesk_agent").rglob("*.py"))
    assert any(p.name == "gmail_tool.py" for p in sources)
    found = [hit for path in sources for hit in _truncations(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))]
    assert found == [], "approval proposals must carry the full content and its sha256:\n" + "\n".join(found)


def test_scan_detects_truncated_and_preview_arguments():
    # Guard against a scan that silently matches nothing.
    probe = (
        "ctx.permissions.verify(proposal('t', 'a', {'body': body[:200]}, 'high', 'r', ctx))\n"
        "ctx.permissions.verify(proposal('t', 'a', self._args(meta, {'x': x[-300:]}), 'high', 'r', ctx))\n"
        "meta = {'text_preview': text}\n"
        "ctx.permissions.verify(proposal('files', 'write_text', {'path': p, 'length': len(text)}, 'high', 'r', ctx))\n"
        "ok = proposal('t', 'a', {'body': body, 'length': len(body)}, 'high', 'r', ctx)\n"
    )
    found = _truncations(probe, "probe")
    assert len(found) == 4
    assert any("len(text) but not text" in hit for hit in found)


# --- helpers ---------------------------------------------------------------

def test_bound_content_fields():
    assert bound_content("body", APPROVED) == {"body": APPROVED, "body_sha256": DIGEST, "body_length": len(APPROVED)}
    assert content_sha256(APPROVED) == DIGEST
    assert json_sha256({"b": 1, "a": [APPROVED]}) == json_sha256({"a": [APPROVED], "b": 1})


# --- per tool ----------------------------------------------------------------

def _assert_bound(permissions: RecordingPermissions, field: str) -> None:
    approved, swapped = permissions.proposals
    assert approved.args[field] == APPROVED
    assert approved.args[f"{field}_sha256"] == DIGEST
    assert approved.args[f"{field}_length"] == len(APPROVED)
    assert not any(key.endswith("_preview") for key in approved.args)
    assert swapped.args[field] == SWAPPED
    assert approved.scope_hash != swapped.scope_hash


class _GmailAdapter:
    cfg = type("Cfg", (), {"enabled": True, "allow_compose": True, "readonly": False, "allow_send": True})()

    def __init__(self) -> None:
        self.sent: list[str] = []

    def build_raw_email(self, to: str, subject: str, body: str) -> dict[str, str]:
        return {"raw": body}

    async def send_email(self, to: str, subject: str, body: str) -> dict[str, str]:
        self.sent.append(body)
        return {"id": "m1"}


@pytest.mark.parametrize("action", ["send_email", "build_raw_email"])
def test_gmail_binds_the_full_body(action):
    permissions = RecordingPermissions()
    adapter = _GmailAdapter()
    tool = GmailTool(adapter)  # type: ignore[arg-type]
    for body in (APPROVED, SWAPPED):
        assert _run(tool, action, {"to": "a@example.test", "subject": "s", "body": body}, permissions).ok
    _assert_bound(permissions, "body")
    if action == "send_email":
        assert adapter.sent == [APPROVED, SWAPPED]


def _browser(monkeypatch) -> tuple[BrowserTool, AsyncMock]:
    tool = BrowserTool(ChromeConfig(enabled=True, allow_evaluate=True))
    monkeypatch.setattr(tool, "_require_enabled", lambda: None)
    monkeypatch.setattr(tool, "_browser_context", AsyncMock(return_value={"actor": "owner"}))
    monkeypatch.setattr(tool, "_query_node_id", AsyncMock(return_value=1))
    monkeypatch.setattr(tool, "_node_center", AsyncMock(return_value=(1.0, 1.0)))
    cdp = AsyncMock(return_value={})
    monkeypatch.setattr(tool, "_cdp", cdp)
    return tool, cdp


def test_browser_evaluate_binds_the_full_script(monkeypatch):
    permissions = RecordingPermissions()
    tool, cdp = _browser(monkeypatch)
    for script in (APPROVED, SWAPPED):
        assert _run(tool, "evaluate", {"expression": script}, permissions).ok
    _assert_bound(permissions, "expression")
    assert cdp.await_args_list[0].args[1]["expression"] == APPROVED


def test_browser_type_selector_binds_the_full_text(monkeypatch):
    permissions = RecordingPermissions()
    tool, cdp = _browser(monkeypatch)
    for text in (APPROVED, SWAPPED):
        assert _run(tool, "type_selector", {"selector": "#q", "text": text}, permissions).ok
    _assert_bound(permissions, "text")
    inserted = [c.args[1]["text"] for c in cdp.await_args_list if c.args[0] == "Input.insertText"]
    assert inserted == [APPROVED, SWAPPED]


class _FakePyAutoGUI:
    def __init__(self) -> None:
        self.written: list[str] = []

    def write(self, text: str, interval: float = 0) -> None:
        self.written.append(text)

    def hotkey(self, *keys: str) -> None:
        pass

    def click(self, **kwargs: Any) -> None:
        pass


def test_computer_type_text_binds_the_full_text(monkeypatch, tmp_path):
    fake = _FakePyAutoGUI()
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    permissions = RecordingPermissions()
    tool = ComputerTool(tmp_path / "screens")
    for text in (APPROVED, SWAPPED):
        assert _run(tool, "type_text", {"text": text, "expected_result": "fill"}, permissions).ok
    _assert_bound(permissions, "text")
    assert fake.written == [APPROVED, SWAPPED]


def test_ui_bridge_type_visible_reply_binds_the_full_text():
    computer = Mock()
    computer.call = AsyncMock(return_value=Mock(ok=True))
    permissions = RecordingPermissions()
    tool = UIBridgeTool(UIBridgeConfig(enabled=True, allowed_apps=["Mail"]), computer)
    for text in (APPROVED, SWAPPED):
        _run(tool, "type_visible_reply", {"app": "Mail", "text": text}, permissions)
    _assert_bound(permissions, "text")
    assert [c.args[2]["text"] for c in computer.call.await_args_list] == [APPROVED, SWAPPED]


def test_pull_request_binds_the_full_body(monkeypatch, tmp_path):
    runs: list[list[str]] = []

    def fake_run(self, args: list[str]):
        runs.append(args)
        return Mock(returncode=0, stdout="https://example.test/pr/1", stderr="")

    monkeypatch.setattr(PullRequestTool, "_run", fake_run)
    permissions = RecordingPermissions()
    tool = PullRequestTool(tmp_path)
    for body in (APPROVED, SWAPPED):
        assert _run(tool, "create", {"title": "t", "body": body, "head": "ai/x"}, permissions).ok
    _assert_bound(permissions, "body")
    assert [r[r.index("--body") + 1] for r in runs] == [APPROVED, SWAPPED]


def test_files_write_binds_the_full_content(tmp_path):
    permissions = RecordingPermissions()
    tool = FilesTool(tmp_path, allow_write=True)
    assert _run(tool, "write_text", {"path": "a.txt", "text": APPROVED}, permissions).ok
    assert _run(tool, "write_text", {"path": "a.txt", "text": SWAPPED}, permissions).ok
    _assert_bound(permissions, "text")
    assert (tmp_path / "a.txt").read_bytes() == SWAPPED.encode("utf-8")


def test_files_write_keeps_the_approved_characters_exactly(tmp_path):
    text = "line1\r\nline2\nline3\r"
    assert _run(FilesTool(tmp_path, allow_write=True), "write_text", {"path": "b.txt", "text": text}, RecordingPermissions()).ok
    assert (tmp_path / "b.txt").read_bytes() == text.encode("utf-8")


def test_vision_binds_the_full_instruction_and_the_image(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "shot.png").write_bytes(PNG)
    router = Mock()
    router.complete = AsyncMock(return_value=ModelResponse(text="{}", provider="fake", model="vision", profile="vision"))
    permissions = RecordingPermissions()
    tool = VisionGroundingTool(router, tmp_path)
    for instruction in (APPROVED, SWAPPED):
        assert _run(tool, "ground", {"image_path": "shot.png", "instruction": instruction}, permissions).ok
    _assert_bound(permissions, "instruction")
    assert permissions.proposals[0].args["image_sha256"] == hashlib.sha256(PNG).hexdigest()
    assert APPROVED in router.complete.await_args_list[0].args[0].user


def test_vision_refuses_an_image_replaced_while_awaiting_approval(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    image = tmp_path / "shot.png"
    image.write_bytes(PNG)
    router = Mock()
    router.complete = AsyncMock()

    class SwapOnApproval(RecordingPermissions):
        def verify(self, action_proposal: Any) -> ApprovalDecision:
            image.write_bytes(PNG + b"other")
            return super().verify(action_proposal)

    with pytest.raises(PermissionError, match="content changed while awaiting approval"):
        _run(VisionGroundingTool(router, tmp_path), "ground", {"image_path": "shot.png", "instruction": "find"}, SwapOnApproval())
    router.complete.assert_not_awaited()


def test_plugin_call_binds_the_full_arguments(monkeypatch, tmp_path):
    entry = tmp_path / "plugin.py"
    entry.write_text("", encoding="utf-8")
    sent: list[bytes] = []

    class Proc:
        returncode = 0

        async def communicate(self, data: bytes):
            sent.append(data)
            return b"{}", b""

    async def fake_exec(*argv, **kwargs):
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    permissions = RecordingPermissions()
    tool = SubprocessPluginTool("deploy", entry, ["plugin.call"])
    for text in (APPROVED, SWAPPED):
        assert _run(tool, "call", {"plugin_action": "post", "plugin_args": {"text": text}}, permissions).ok
    approved, swapped = permissions.proposals
    assert approved.args["plugin_args"] == {"text": APPROVED}
    assert approved.args["plugin_args_sha256"] != swapped.args["plugin_args_sha256"]
    assert approved.scope_hash != swapped.scope_hash
    assert APPROVED.encode("utf-8") in sent[0]


# --- approval resume -------------------------------------------------------

def test_approved_write_cannot_be_resumed_with_different_content(tmp_path):
    cfg = PermissionConfig(approval_mode="remote_approval", audit_log=tmp_path / "audit.log")
    permissions = PermissionManager(cfg, ApprovalStore(tmp_path / "approvals.sqlite3"))
    workspace = tmp_path / "ws"
    tool = FilesTool(workspace, allow_write=True)

    with pytest.raises(ApprovalRequired) as required:
        _run(tool, "write_text", {"path": "notes.txt", "text": APPROVED}, permissions)
    assert required.value.proposal["args"]["text"] == APPROVED
    permissions.allow_approved_proposal(required.value.proposal)

    # Same step resumed with content that shares the old preview and length.
    with pytest.raises(ApprovalRequired):
        _run(tool, "write_text", {"path": "notes.txt", "text": SWAPPED}, permissions)
    assert not (workspace / "notes.txt").exists()

    assert _run(tool, "write_text", {"path": "notes.txt", "text": APPROVED}, permissions).ok
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == APPROVED


def test_approved_email_cannot_be_resumed_with_different_body(tmp_path):
    cfg = PermissionConfig(approval_mode="remote_approval", audit_log=tmp_path / "audit.log")
    permissions = PermissionManager(cfg, ApprovalStore(tmp_path / "approvals.sqlite3"))
    adapter = _GmailAdapter()
    tool = GmailTool(adapter)  # type: ignore[arg-type]
    args = {"to": "a@example.test", "subject": "s"}

    with pytest.raises(ApprovalRequired) as required:
        _run(tool, "send_email", {**args, "body": APPROVED}, permissions)
    permissions.allow_approved_proposal(required.value.proposal)
    with pytest.raises(ApprovalRequired):
        _run(tool, "send_email", {**args, "body": SWAPPED}, permissions)
    assert adapter.sent == []
    assert _run(tool, "send_email", {**args, "body": APPROVED}, permissions).ok
    assert adapter.sent == [APPROVED]
