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
import threading
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
    for title in ("WeChat - 扫码登录", "LINE Login", "Sign in to WhatsApp", "WhatsApp - Verification code"):
        app = "WeChat" if "WeChat" in title else ("LINE" if "LINE" in title else "WhatsApp")
        tool, computer = bridge([ForegroundWindow(title=title)])
        with pytest.raises(PermissionError, match="sign-in screen"):
            run(tool, "type_visible_reply", {"app": app, "text": "password123"})
        assert computer.calls == []


ACTIONS = [
    ("observe", {"app": "WhatsApp"}, "screenshot"),
    ("click", {"app": "WhatsApp", "x": 10, "y": 20}, "click"),
    ("type_visible_reply", {"app": "WhatsApp", "text": "meet at 5"}, "type_text"),
    ("press_send", {"app": "WhatsApp"}, "hotkey"),
]


@pytest.mark.parametrize(("action", "args", "computer_action"), ACTIONS)
def test_focus_that_moves_during_the_approval_wait_stops_the_action(action, args, computer_action):
    # Before the approval request WhatsApp is in front; when the approval comes back, a terminal is.
    tool, computer = bridge([ForegroundWindow(app="WhatsApp"), ForegroundWindow(app="Terminal")])
    approver = Approver()
    with pytest.raises(PermissionError, match="WhatsApp is not the foreground window"):
        run(tool, action, args, approver)
    assert len(approver.proposals) == 1  # refused after the approval, before any input
    assert computer.calls == []


@pytest.mark.parametrize(("action", "args", "computer_action"), ACTIONS)
def test_the_approver_sees_which_window_will_receive_the_action(action, args, computer_action):
    window = ForegroundWindow(app="WhatsApp", bundle_id="net.whatsapp.WhatsApp")
    tool, computer = bridge([window])
    approver = Approver()
    assert run(tool, action, args, approver).ok
    assert f"(foreground: {window.describe()})" in approver.proposals[0].reason
    assert [call[0] for call in computer.calls] == [computer_action]


def test_the_probe_runs_off_the_event_loop():
    # A hung lsappinfo/xdotool must not stall the daemon's event loop.
    threads = []

    def probe():
        threads.append(threading.current_thread())
        return ForegroundWindow(app="WhatsApp")

    tool = UIBridgeTool(UIBridgeConfig(enabled=True, allowed_apps=["WhatsApp"]), Computer(), foreground_probe=probe)  # type: ignore[arg-type]
    assert run(tool, "press_send", {"app": "WhatsApp"}).ok
    assert len(threads) == 2 and threading.main_thread() not in threads


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
        # macOS, as lsappinfo reports them (WhatsApp's name starts with U+200E).
        (ForegroundWindow(app="\u200eWhatsApp", bundle_id="net.whatsapp.WhatsApp"), "WhatsApp", True),
        (ForegroundWindow(app="\u200eWhatsApp", bundle_id="net.whatsapp.WhatsApp"), "WhatsApp Business", False),
        (ForegroundWindow(app="访达", bundle_id="com.apple.finder"), "WeChat", False),
        (ForegroundWindow(app="QQ", bundle_id="com.tencent.qq"), "QQ", True),
        (ForegroundWindow(app="X"), "X", True),
        # Other apps' bundle ids: matched whole, never as substrings.
        (ForegroundWindow(app="Online Banking", bundle_id="com.example.onlinebanking"), "LINE", False),
        (ForegroundWindow(app="OmniOutliner", bundle_id="com.omnigroup.OmniOutliner5"), "LINE", False),
        (ForegroundWindow(app="Timeline", bundle_id="com.example.timeline"), "LINE", False),
        (ForegroundWindow(app="Linear", bundle_id="com.linear"), "LINE", False),
        (ForegroundWindow(app="Messenger", bundle_id="com.facebook.archon"), "Facebook", False),
        (ForegroundWindow(app="Xcode", bundle_id="com.apple.dt.Xcode"), "X", False),
        (ForegroundWindow(app="QQ音乐", bundle_id="com.tencent.QQMusicMac"), "QQ", False),
        (ForegroundWindow(app="GitHub", bundle_id="com.google.Chrome.app.abcdefghijklmnop"), "Chrome", False),
        # A browser on macOS reports no title, so a web app cannot be confirmed there.
        (ForegroundWindow(app="Google Chrome", bundle_id="com.google.Chrome"), "WhatsApp", False),
        # X11: the window class names the app; another app's class vetoes its title.
        (ForegroundWindow(app="Gnome-terminal", title="user@host: ~/src/line-items"), "LINE", False),
        (ForegroundWindow(app="Gnome-terminal", title="vim signal-notes.md"), "Signal", False),
        (ForegroundWindow(app="Gnome-terminal", title='omnidesk run "reply to Bob on WhatsApp"'), "WhatsApp", False),
        (ForegroundWindow(app="Gnome-terminal", title="WhatsApp"), "WhatsApp", False),
        (ForegroundWindow(app="Google-chrome", title="WhatsApp - Google Chrome"), "WhatsApp", True),
        (ForegroundWindow(app="Google-chrome", title="Home / X - Google Chrome"), "X", True),
        # Title only (Windows): a whole title part must be the app's name.
        (ForegroundWindow(title="WhatsApp Business"), "WhatsApp", False),
        (ForegroundWindow(title="系统信息"), "Messages", False),
        (ForegroundWindow(title="微信支付"), "WeChat", False),
        (ForegroundWindow(title="微信"), "WeChat", True),
        (ForegroundWindow(title="Telegram (12)"), "Telegram", True),
        (ForegroundWindow(title="Chat | Microsoft Teams"), "Microsoft Teams", True),
    ],
)
def test_window_matching(window, app, matches):
    assert window_matches_app(window, app) is matches


@pytest.mark.parametrize(
    "title",
    ["钉钉 - 登录", "Verification code", "2-Step Verification", "安全验证", "身份验证 - 钉钉", "Enter password", "Enter passcode", "Two-factor authentication"],
)
def test_sign_in_and_verification_titles_are_detected(title):
    assert looks_like_sign_in(ForegroundWindow(title=title))


def test_ordinary_windows_are_not_sign_in_screens():
    assert not looks_like_sign_in(ForegroundWindow(title="WhatsApp"))
    assert not looks_like_sign_in(ForegroundWindow(title="general (Channel) - Acme - Slack"))
    assert not looks_like_sign_in(ForegroundWindow(app="WhatsApp"))  # no title: nothing to judge


# Captured with `lsappinfo front` and `lsappinfo info -only name -only bundleid <ASN>`
# on macOS 27.2.1 (Darwin 27.2.0); only the ASN was replaced.
LSAPPINFO_FRONT = "ASN:0x0-0x1d01d:\n"
LSAPPINFO_WHATSAPP = '"\u200eWhatsApp" ASN:0x0-0x1d01d: \n    bundleID="net.whatsapp.WhatsApp"\n    bundle path=[ NULL ] \n    executable path=[ NULL ] \n !cgsConnection !signalled type=[ NULL ]  flavor=[ NULL ]  Version=[ NULL ]  Arch=!!none \n\n'
LSAPPINFO_FINDER = '"访达" ASN:0x0-0x1d01d: \n    bundleID="com.apple.finder"\n    bundle path=[ NULL ] \n    executable path=[ NULL ] \n !cgsConnection !signalled type=[ NULL ]  flavor=[ NULL ]  Version=[ NULL ]  Arch=!!none \n\n'
# The same query with -long, also captured there.
LSAPPINFO_FINDER_LONG = '"CFBundleIdentifier"="com.apple.finder"\n"LSASN"=ASN:0x0-0x1d01d:\n"LSDisplayName"="访达"\n'


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        (LSAPPINFO_WHATSAPP, ForegroundWindow(app="\u200eWhatsApp", bundle_id="net.whatsapp.WhatsApp")),
        (LSAPPINFO_FINDER, ForegroundWindow(app="访达", bundle_id="com.apple.finder")),
        # For the app that is in front, the same host ends the header line with "(in front)".
        (LSAPPINFO_WHATSAPP.replace(": \n", ": (in front) \n", 1), ForegroundWindow(app="\u200eWhatsApp", bundle_id="net.whatsapp.WhatsApp")),
        (LSAPPINFO_FINDER_LONG, ForegroundWindow(app="访达", bundle_id="com.apple.finder")),
    ],
)
def test_macos_probe_reads_lsappinfo(monkeypatch, info, expected):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=LSAPPINFO_FRONT if argv[1] == "front" else info)

    monkeypatch.setattr(foreground.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(foreground.subprocess, "run", fake_run)
    assert probe_foreground() == expected
    # The info query asks about the app `front` named, and only for its name and bundle id.
    assert calls == [["lsappinfo", "front"], ["lsappinfo", "info", "-only", "name", "-only", "bundleid", "ASN:0x0-0x1d01d:"]]


def test_a_real_macos_reading_confirms_the_named_app_only(monkeypatch):
    monkeypatch.setattr(foreground.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(foreground.subprocess, "run", lambda argv, **kw: SimpleNamespace(stdout=LSAPPINFO_FRONT if argv[1] == "front" else LSAPPINFO_WHATSAPP))
    window = probe_foreground()
    assert window is not None
    assert window_matches_app(window, "WhatsApp")
    assert not window_matches_app(window, "WhatsApp Business") and not window_matches_app(window, "WeChat")


def test_an_lsappinfo_reading_without_name_or_bundle_is_unknown():
    assert foreground.parse_lsappinfo_info("") is None
    assert foreground.parse_lsappinfo_info("    bundle path=[ NULL ] \n") is None


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
