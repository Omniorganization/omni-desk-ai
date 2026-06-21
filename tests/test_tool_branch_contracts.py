from __future__ import annotations

import pytest

from omnidesk_agent.config import ChromeConfig
from omnidesk_agent.core.models import ApprovalDecision
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.browser import BrowserTool
from omnidesk_agent.tools.channel_send import ChannelSendTool
from omnidesk_agent.tools.computer import ComputerTool
from omnidesk_agent.tools.gmail_tool import GmailTool


class Permissions:
    def __init__(self, mode="allow"):
        self.mode = mode
        self.proposals = []

    def verify(self, proposal):
        self.proposals.append(proposal)
        return ApprovalDecision(self.mode != "deny", self.mode, "test")


def _ctx(mode="allow"):
    return ToolContext(permissions=Permissions(mode))


class SendAdapter:
    def __init__(self):
        self.sent = []

    async def send_text(self, recipient, text, **kwargs):
        self.sent.append((recipient, text, kwargs))


class GmailAdapter:
    def __init__(self):
        self.oauth = type("OAuth", (), {
            "run_local_flow": lambda self, port=0: {"access_token": "token"},
            "build_authorization_url": lambda self, redirect_uri, state, actor=None: {"url": redirect_uri, "state": state, "actor": actor},
            "exchange_code": lambda self, code, redirect_uri, state=None, actor=None: {"code": code, "state": state, "actor": actor},
        })()

    def configured(self):
        return True

    def authenticated(self):
        return False

    def build_raw_email(self, to, subject, body):
        return {"raw": f"{to}|{subject}|{body}"}

    async def send_email(self, to, subject, body):
        return {"sent": True, "to": to, "subject": subject, "body": body}


@pytest.mark.asyncio
async def test_channel_send_text_and_email_branches():
    adapter = SendAdapter()
    gmail = GmailAdapter()
    tool = ChannelSendTool({"telegram": adapter, "gmail": gmail})

    sent = await tool.call("send_text", {"channel": "telegram", "recipient": "u1", "text": "hello", "options": {"silent": True}}, _ctx())
    assert sent.ok is True
    assert adapter.sent == [("u1", "hello", {"silent": True})]

    dry = await tool.call("send_text", {"channel": "telegram", "recipient": "u1", "text": "hello"}, _ctx("dry_run"))
    assert dry.ok is False
    assert "dry-run" in dry.summary

    missing = await tool.call("send_text", {"channel": "missing", "recipient": "u1", "text": "hello"}, _ctx())
    assert missing.ok is False
    assert "Unknown channel" in missing.error

    email = await tool.call("send_email", {"to": "a@example.com", "subject": "Hi", "body": "Body"}, _ctx())
    assert email.ok is True
    assert email.data["raw"] == "a@example.com|Hi|Body"

    with pytest.raises(ValueError, match="Unsupported channels action"):
        await tool.call("unknown", {}, _ctx())


@pytest.mark.asyncio
async def test_gmail_tool_oauth_and_email_branches():
    tool = GmailTool(GmailAdapter())
    assert (await tool.call("configured", {}, _ctx())).data == {"configured": True, "authenticated": False}
    assert (await tool.call("auth_local", {"port": 9999}, _ctx())).data["keys"] == ["access_token"]
    assert (await tool.call("auth_url", {"redirect_uri": "http://localhost/cb"}, _ctx())).data["state"] == "omnidesk-gmail"
    assert (await tool.call("auth_callback", {"code": "c", "redirect_uri": "http://localhost/cb", "state": "s"}, _ctx())).data["keys"] == ["actor", "code", "state"]
    assert (await tool.call("build_raw_email", {"to": "a@example.com", "subject": "S", "body": "B"}, _ctx())).data["raw"] == "a@example.com|S|B"
    assert (await tool.call("send_email", {"to": "a@example.com", "subject": "S", "body": "B"}, _ctx())).data["sent"] is True
    with pytest.raises(ValueError, match="Unsupported gmail action"):
        await tool.call("unknown", {}, _ctx())


class FakeBrowserTool(BrowserTool):
    async def _tabs(self):
        return [{"id": "t1", "title": "Title", "url": "https://allowed.test/page"}]

    async def _cdp(self, method, params=None, target_id=None):
        if method == "DOM.getDocument":
            return {"root": {"nodeId": 1}}
        if method == "DOM.querySelector":
            return {"nodeId": 2}
        if method == "DOM.getOuterHTML":
            return {"outerHTML": "<html><body>visible text</body></html>"}
        if method == "DOM.getBoxModel":
            return {"model": {"content": [10, 10, 30, 10, 30, 30, 10, 30]}}
        if method == "Runtime.evaluate" and "innerText" in str((params or {}).get("expression")):
            return {"result": {"value": "visible text"}}
        if method == "Page.captureScreenshot":
            return {"data": "png-data"}
        return {"method": method, "params": params or {}, "target_id": target_id}


class RiskyBrowserTool(FakeBrowserTool):
    async def _tabs(self):
        return [{"id": "risk-tab", "title": "Ads Manager", "url": "https://business.facebook.com/adsmanager"}]


@pytest.mark.asyncio
async def test_browser_tool_validation_and_action_branches():
    cfg = ChromeConfig(enabled=True, allowed_origins=["https://allowed.test"], allow_evaluate=True)
    tool = FakeBrowserTool(cfg)

    assert len((await tool.call("list_tabs", {}, _ctx())).data) == 1
    assert (await tool.call("navigate", {"url": "https://allowed.test/next"}, _ctx())).data["method"] == "Page.navigate"
    assert (await tool.call("get_dom_text", {}, _ctx())).data["text"] == "visible text"
    assert (await tool.call("click_selector", {"selector": "#go"}, _ctx())).ok is True
    assert (await tool.call("type_selector", {"selector": "#name", "text": "Ada"}, _ctx())).ok is True
    assert (await tool.call("evaluate", {"expression": "1 + 1"}, _ctx())).data["method"] == "Runtime.evaluate"
    assert (await tool.call("screenshot", {}, _ctx())).data["png_base64"] == "png-data"

    with pytest.raises(ValueError, match="Browser origin not allowed"):
        tool._check_url("https://blocked.test")
    with pytest.raises(ValueError, match="allowlist is empty"):
        BrowserTool(ChromeConfig())._check_url("https://allowed.test")
    with pytest.raises(PermissionError, match="disabled"):
        BrowserTool(ChromeConfig(allowed_origins=["https://allowed.test"]))._check_js("1 + 1")
    with pytest.raises(PermissionError, match="document.cookie"):
        tool._check_js("document.cookie")
    with pytest.raises(ValueError, match="Unsupported browser action"):
        await tool.call("unknown", {}, _ctx())


@pytest.mark.asyncio
async def test_browser_high_risk_context_and_actor_binding():
    cfg = ChromeConfig(enabled=True, allowed_origins=["https://business.facebook.com"], allow_evaluate=True)
    tool = RiskyBrowserTool(cfg)
    permissions = Permissions()
    ctx = ToolContext(permissions=permissions, actor="alice")

    assert (await tool.call("click_selector", {"selector": "#publish"}, ctx)).ok is True
    proposal = permissions.proposals[-1]
    assert proposal.risk == "critical"
    assert proposal.args["selector"] == "#publish"
    assert proposal.args["url"] == "https://business.facebook.com/adsmanager"
    assert proposal.args["origin"] == "https://business.facebook.com"
    assert proposal.args["title"] == "Ads Manager"
    assert proposal.args["actor"] == "alice"
    assert proposal.args["high_risk"] is True

    with pytest.raises(PermissionError, match="already bound to actor alice"):
        await tool.call("screenshot", {}, ToolContext(permissions=Permissions(), actor="bob"))


@pytest.mark.asyncio
async def test_computer_tool_dry_run_branches(tmp_path):
    tool = ComputerTool(tmp_path / "screens")
    assert (await tool.call("screenshot", {"expected_result": "inspect"}, _ctx("dry_run"))).ok is False
    assert (await tool.call("click", {"x": 1, "y": 2, "expected_result": "open"}, _ctx("dry_run"))).ok is False
    assert (await tool.call("move", {"x": 1, "y": 2, "expected_result": "hover"}, _ctx("dry_run"))).ok is False
    assert (await tool.call("type_text", {"text": "abc", "expected_result": "fill"}, _ctx("dry_run"))).ok is False
    assert (await tool.call("hotkey", {"keys": ["cmd", "l"], "expected_result": "focus"}, _ctx("dry_run"))).ok is False
    with pytest.raises(ValueError, match="requires expected_result"):
        await tool.call("click", {"x": 1, "y": 2}, _ctx("dry_run"))
    with pytest.raises(ValueError, match="hotkey requires keys"):
        await tool.call("hotkey", {"keys": [], "expected_result": "focus"}, _ctx("dry_run"))
    with pytest.raises(ValueError, match="Unsupported computer action"):
        await tool.call("unknown", {}, _ctx("dry_run"))
