"""The UI bridge acts only in the named, allowed app while it is the visible,
signed-in foreground window.

Before this change an empty `app` skipped the allowlist, and nothing checked
which window had focus or whether it showed a sign-in screen: an approved
"type the reply" could go into whatever window was in front, including a
login form. require_foreground_confirmation existed in config but did nothing.
"""
from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from omnidesk_agent.config import UIBridgeConfig
from omnidesk_agent.core.models import ApprovalDecision, ToolResult
from omnidesk_agent.tools import foreground
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.foreground import ForegroundWindow, looks_like_sign_in, probe_foreground, window_matches_app
from omnidesk_agent.tools.ui_bridge_tool import UIBridgeTool


class Computer:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call(self, tool, action, args, ctx):
        self.calls.append((action, args))
        return ToolResult(True, summary=f"{action} done")


class Approver:
    def __init__(self):
        self.proposals = []

    def verify(self, proposal):
        self.proposals.append(proposal)
        return ApprovalDecision(True, "allow", "ok")


def bridge(windows, *, require=True, allowed=("WhatsApp", "WeChat", "LINE")):
    """A bridge whose foreground probe returns `windows` in order (the last one repeats)."""
    sequence = list(windows)

    def probe():
        return sequence.pop(0) if len(sequence) > 1 else sequence[0]

    computer = Computer()
    tool = UIBridgeTool(UIBridgeConfig(enabled=True, require_foreground_confirmation=require, allowed_apps=list(allowed)), computer, foreground_probe=probe)  # type: ignore[arg-type]
    return tool, computer


def run(tool, action, args, approver=None):
    ctx = ToolContext(permissions=approver or Approver(), source="local-cli", actor="owner")
    return asyncio.run(tool.call(action, args, ctx))


def test_an_action_must_name_an_allowed_app():
    tool, computer = bridge([ForegroundWindow(app="WhatsApp")])
    for args in ({"text": "hi"}, {"app": "  ", "text": "hi"}):
        with pytest.raises(PermissionError, match="must name the target app"):
            run(tool, "type_visible_reply", args)
    with pytest.raises(PermissionError, match="not allowed"):
        run(tool, "type_visible_reply", {"app": "Banking", "text": "hi"})
    assert computer.calls == []


def test_input_goes_only_to_the_named_app_in_the_foreground():
    tool, computer = bridge([ForegroundWindow(app="Notes")])
    with pytest.raises(PermissionError, match="WhatsApp is not the foreground window"):
        run(tool, "type_visible_reply", {"app": "WhatsApp", "text": "meet at 5"})
    assert computer.calls == []

    tool, computer = bridge([ForegroundWindow(app="WhatsApp")])
    approver = Approver()
    assert run(tool, "type_visible_reply", {"app": "WhatsApp", "text": "meet at 5"}, approver).ok
    assert computer.calls == [("type_text", {"text": "meet at 5", "expected_result": "Type visible reply in WhatsApp"})]
    assert "foreground: WhatsApp" in approver.proposals[0].reason  # the approver sees where it goes


def test_an_unknown_foreground_window_is_refused():
    tool, computer = bridge([None])  # type: ignore[list-item]
    with pytest.raises(PermissionError, match="cannot confirm"):
        run(tool, "press_send", {"app": "WhatsApp"})
    assert computer.calls == []


def test_a_sign_in_screen_is_never_typed_into():
    for title in ("WeChat - 扫码登录", "LINE Login", "Sign in to WhatsApp"):
        app = "WeChat" if "WeChat" in title else ("LINE" if "LINE" in title else "WhatsApp")
        tool, computer = bridge([ForegroundWindow(title=title)])
        with pytest.raises(PermissionError, match="sign-in screen"):
            run(tool, "type_visible_reply", {"app": app, "text": "password123"})
        assert computer.calls == []


def test_focus_that_moves_during_the_approval_wait_stops_the_action():
    # Before the approval request WhatsApp is in front; when the approval comes back, a terminal is.
    tool, computer = bridge([ForegroundWindow(app="WhatsApp"), ForegroundWindow(app="Terminal")])
    with pytest.raises(PermissionError, match="not the foreground window"):
        run(tool, "click", {"app": "WhatsApp", "x": 10, "y": 20})
    assert computer.calls == []


def test_foreground_confirmation_can_be_turned_off_for_development():
    probe_calls = []
    tool = UIBridgeTool(UIBridgeConfig(enabled=True, require_foreground_confirmation=False, allowed_apps=["WhatsApp"]), Computer(), foreground_probe=lambda: probe_calls.append(1))  # type: ignore[arg-type]
    assert run(tool, "observe", {"app": "WhatsApp"}).ok
    assert probe_calls == []


@pytest.mark.parametrize(
    ("window", "app", "matches"),
    [
        (ForegroundWindow(app="微信"), "WeChat", True),
        (ForegroundWindow(bundle_id="com.tencent.xinWeChat"), "WeChat", True),
        (ForegroundWindow(app="Google Chrome"), "Chrome", True),
        (ForegroundWindow(title="Slack | general | Acme"), "Slack", True),
        (ForegroundWindow(title="Online banking"), "LINE", False),
        (ForegroundWindow(title="LINE"), "LINE", True),
        (ForegroundWindow(title="Page 1 of X"), "X", False),
        (ForegroundWindow(title="Home - X"), "X", True),
        (ForegroundWindow(app="WhatsApp"), "WhatsApp Business", False),
        (ForegroundWindow(), "WhatsApp", False),
    ],
)
def test_window_matching(window, app, matches):
    assert window_matches_app(window, app) is matches


def test_sign_in_detection():
    assert looks_like_sign_in(ForegroundWindow(title="钉钉 - 登录"))
    assert not looks_like_sign_in(ForegroundWindow(title="WhatsApp"))
    assert not looks_like_sign_in(ForegroundWindow(app="WhatsApp"))  # no title: nothing to judge


def test_macos_probe_reads_lsappinfo(monkeypatch):
    outputs = {"front": "ASN:0x0-0x1d01d:\n", "info": '"LSDisplayName"="WeChat"\n"CFBundleIdentifier"="com.tencent.xinWeChat"\n'}

    def fake_run(argv, **kwargs):
        return SimpleNamespace(stdout=outputs["front" if argv[1] == "front" else "info"])

    monkeypatch.setattr(foreground.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(foreground.subprocess, "run", fake_run)
    assert probe_foreground() == ForegroundWindow(app="WeChat", bundle_id="com.tencent.xinWeChat")


def test_linux_probe_reads_xdotool_and_fails_closed_without_it(monkeypatch):
    monkeypatch.setattr(foreground.platform, "system", lambda: "Linux")
    monkeypatch.setattr(foreground.subprocess, "run", lambda argv, **kw: SimpleNamespace(stdout={"getwindowname": "Slack | general\n", "getwindowclassname": "Slack\n"}[argv[2]]))
    assert probe_foreground() == ForegroundWindow(app="Slack", title="Slack | general")

    def missing(argv, **kwargs):
        raise FileNotFoundError("xdotool")

    monkeypatch.setattr(foreground.subprocess, "run", missing)
    assert probe_foreground() is None


def test_probe_errors_and_unknown_platforms_mean_unknown(monkeypatch):
    def failing(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(foreground.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(foreground.subprocess, "run", failing)
    assert probe_foreground() is None
    monkeypatch.setattr(foreground.platform, "system", lambda: "Plan9")
    assert probe_foreground() is None
