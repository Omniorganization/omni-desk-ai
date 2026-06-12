from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from omnidesk_agent.config import AppConfig, ChromeConfig, GmailConfig
from omnidesk_agent.core.models import ApprovalDecision
from omnidesk_agent.observability import MetricsRegistry
from omnidesk_agent.server_routes.admin_routes import _runtime_slo_snapshot
from omnidesk_agent.self_upgrade.governance import GovernedSelfImprovement
from omnidesk_agent.self_upgrade.patcher import UpgradePatcher
from omnidesk_agent.self_upgrade.proposal.proposal_schema import UpgradeProposal
from omnidesk_agent.self_upgrade.state_machine import UpgradeStateMachine, normalize_upgrade_checks
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.browser import BrowserTool
from omnidesk_agent.tools.channel_send import ChannelSendTool
from omnidesk_agent.tools.computer import ComputerTool
from omnidesk_agent.tools.gmail_tool import GmailTool
from omnidesk_agent.validation.production import validate_production_config


class Perms:
    def __init__(self, mode: str = "allow"):
        self.mode = mode

    def verify(self, proposal):
        return ApprovalDecision(self.mode != "dry_run", self.mode, "ok")


def ctx(mode: str = "allow"):
    return ToolContext(permissions=Perms(mode), source="unit", actor="tester")


class DomBrowser(BrowserTool):
    async def _tabs(self):
        return [{"id": "t", "title": "T", "url": "https://dom.test", "webSocketDebuggerUrl": "ws://dom"}]

    async def _cdp(self, method, params=None, target_id=None):
        if method == "DOM.getDocument":
            return {"root": {"nodeId": 1}}
        if method == "DOM.querySelector":
            return {"nodeId": 2}
        if method == "DOM.getOuterHTML":
            return {"outerHTML": "<html><script>x()</script><body><b>Hello</b> DOM</body></html>"}
        if method == "DOM.getBoxModel":
            return {"model": {"content": [0, 0, 20, 0, 20, 10, 0, 10]}}
        if method in {"Input.dispatchMouseEvent", "Input.insertText", "Page.navigate", "Page.captureScreenshot"}:
            return {"method": method, "params": params or {}}
        raise AssertionError(f"unexpected method {method}")


def test_browser_dom_input_paths_and_disabled_gate():
    async def run_case():
        tool = DomBrowser(ChromeConfig(enabled=True, allowed_origins=["https://dom.test"]))
        text = await tool.call("get_dom_text", {}, ctx())
        assert text.data["text"] == "Hello DOM"
        assert text.data["method"] == "DOM.getOuterHTML"
        assert (await tool.call("click_selector", {"selector": "button"}, ctx())).data["method"] == "DOM/Input"
        assert (await tool.call("type_selector", {"selector": "input", "text": "abc"}, ctx())).data["method"] == "DOM/Input"
        disabled = DomBrowser(ChromeConfig(enabled=False, allowed_origins=["https://dom.test"]))
        with pytest.raises(PermissionError, match="disabled"):
            await disabled.call("list_tabs", {}, ctx())

    asyncio.run(run_case())


class FakeImage:
    width = 200
    height = 100

    def resize(self, size):
        img = FakeImage()
        img.width, img.height = size
        return img

    def save(self, fp, format="PNG"):
        fp.write(b"fake-png")


class FakePyAutoGUI:
    def __init__(self):
        self.actions = []

    def screenshot(self):
        return FakeImage()

    def click(self, **kwargs):
        self.actions.append(("click", kwargs))

    def typewrite(self, text, interval=0):
        self.actions.append(("typewrite", text, interval))

    def write(self, text, interval=0):
        self.actions.append(("write", text, interval))

    def hotkey(self, *keys):
        self.actions.append(("hotkey", keys))

    def moveTo(self, x, y, duration=0):
        self.actions.append(("moveTo", x, y, duration))


def test_computer_non_dry_actions_cover_runtime(monkeypatch, tmp_path: Path):
    async def run_case():
        fake = FakePyAutoGUI()
        monkeypatch.setitem(sys.modules, "pyautogui", fake)
        tool = ComputerTool(tmp_path / "screens")
        shot = await tool.call("screenshot", {"expected_result": "inspect", "max_width": 100, "return_base64": True}, ctx())
        assert shot.ok and Path(shot.data["image_path"]).exists()
        assert shot.data["base64_returned"] is True
        assert (await tool.call("click", {"x": 1, "y": 2, "expected_result": "open"}, ctx())).ok
        assert (await tool.call("type_text", {"text": "abc", "expected_result": "fill"}, ctx())).ok
        assert (await tool.call("hotkey", {"keys": ["ctrl", "l"], "expected_result": "focus"}, ctx())).ok
        assert (await tool.call("move", {"x": 3, "y": 4, "expected_result": "hover"}, ctx())).ok
        assert fake.actions

    asyncio.run(run_case())


class GmailAdapterWithCfg:
    def __init__(self, cfg):
        self.cfg = cfg
        self.oauth = object()

    def configured(self):
        return True

    def authenticated(self):
        return True


class AdapterWithCfg:
    def __init__(self, enabled: bool):
        self.cfg = type("Cfg", (), {"enabled": enabled})()

    async def send_text(self, recipient, text, **kwargs):
        return {"id": "sent"}


def test_tool_internal_config_gates_are_hard_failures():
    async def run_case():
        gmail_cfg = GmailConfig(enabled=True, readonly=True, allow_send=False, allow_compose=False)
        gmail = GmailTool(GmailAdapterWithCfg(gmail_cfg))
        with pytest.raises(PermissionError, match="compose"):
            await gmail.call("build_raw_email", {"to": "a", "subject": "s", "body": "b"}, ctx())
        with pytest.raises(PermissionError, match="send"):
            await gmail.call("send_email", {"to": "a", "subject": "s", "body": "b"}, ctx())
        channels = ChannelSendTool({"telegram": AdapterWithCfg(False)})
        with pytest.raises(PermissionError, match="disabled"):
            await channels.call("send_text", {"channel": "telegram", "recipient": "r", "text": "x"}, ctx())

    asyncio.run(run_case())


def test_upgrade_checks_contract_and_safe_patcher_path(tmp_path: Path):
    proposal = UpgradeProposal(title="t", source="s", problem="p", proposed_change="c", expected_benefit="b")
    proposal.metadata = {
        "state": "CANARY",
        "checks": {
            "regression": {"ok": True},
            "security": {"ok": True},
            "human_review": {"decision": "approved"},
        },
    }
    UpgradeStateMachine().assert_can_promote_to_pr(proposal.to_dict())
    checks = normalize_upgrade_checks({"regression_result": {"ok": True}, "security_result": {"ok": False}})
    assert checks["regression"]["ok"] is True and checks["security"]["ok"] is False
    patcher = UpgradePatcher(tmp_path)
    with pytest.raises(ValueError, match="outside repository"):
        patcher._safe_repo_path("../outside")


def test_production_rejects_subprocess_plugins_when_enabled():
    cfg = AppConfig()
    cfg.channels.chrome.enabled = False
    cfg.memory_privacy.encrypt_at_rest = True
    cfg.sandbox.docker_image = "python:3.11-slim@sha256:" + '66f011380d0e49ed280c789fbd08ff0d40968ee7b665575489afa95c98196ab5'
    cfg.plugins.default_sandbox = "subprocess"
    result = validate_production_config(cfg, {
        "OMNIDESK_ENV": "production",
        "OMNIDESK_ADMIN_TOKEN": "x" * 40,
        "OMNIDESK_GATEWAY_SECRET": "x" * 40,
        "OMNIDESK_MEMORY_ENCRYPTION_KEY": "x" * 40,
        "OMNIDESK_PLUGIN_SIGNING_SECRET": "x" * 40,
    })
    assert "plugins.default_sandbox must be docker in production" in result["issues"]

class SLOJobQueue:
    def stats(self):
        return {"completed": 99, "dead_letter": 1}


class SLOOutbound:
    def stats(self):
        return {"sent": 10, "dead_letter": 0}


class SLORuntime:
    job_queue = SLOJobQueue()
    outbound_messages = SLOOutbound()


def test_runtime_slo_snapshot_uses_real_metrics_not_static_defaults():
    metrics = MetricsRegistry()
    for _ in range(100):
        metrics.inc("omnidesk_webhook_enqueue_attempts_total")
    metrics.inc("omnidesk_webhook_enqueue_failures_total")
    for _ in range(10):
        metrics.inc("omnidesk_resume_attempts_total")
    for _ in range(9):
        metrics.inc("omnidesk_resume_success_total")
    for _ in range(20):
        metrics.inc("omnidesk_planner_requests_total")
    metrics.inc("planner_fallback_total")
    for _ in range(100):
        metrics.inc("omnidesk_tool_calls_total", status="ok")
    metrics.inc("omnidesk_tool_calls_total", status="exception")
    for _ in range(50):
        metrics.inc("omnidesk_plugin_call_total", status="ok")
    metrics.inc("omnidesk_plugin_call_total", status="timeout")

    snapshot = _runtime_slo_snapshot(SLORuntime(), metrics)

    assert snapshot["webhook_enqueue_success_rate"] == 0.99
    assert snapshot["approval_resume_success_rate"] == 0.9
    assert snapshot["planner_fallback_rate"] == 0.05
    assert snapshot["tool_error_rate"] == 1 / 101
    assert snapshot["plugin_timeout_rate"] == 1 / 51
    assert snapshot["job_dead_letter_rate"] == 0.01

def test_governance_evaluation_writes_state_machine_checks_contract(tmp_path: Path):
    async def run_case():
        gov = GovernedSelfImprovement(tmp_path / "ws", tmp_path / "repo")
        proposal = UpgradeProposal(
            title="Improve selector", source="unit", problem="selector drift", proposed_change="patch", expected_benefit="less failure",
            upgrade_type="skill", risk_level="low", rollback_plan="revert", affected_modules=["browser"],
        )
        gov.proposal_store.create(proposal)

        async def ok_run():
            return {"ok": True, "report_path": "report.json"}

        gov.regression_runner.run = ok_run
        gov.security_runner.run = ok_run
        gov.risk_classifier.classify = lambda *a, **k: {"can_auto_canary": True, "requires_human_approval": False}
        result = await gov.evaluate_proposal(proposal.proposal_id, allow_canary=True)
        stored = gov.proposal_store.get(proposal.proposal_id)
        assert result["evaluation"]["checks"]["regression"]["ok"] is True
        assert stored.metadata["checks"]["security"]["ok"] is True
        assert stored.metadata["state"] == "CANARY"
        stored.metadata["checks"]["human_review"] = {"decision": "approved"}
        UpgradeStateMachine().assert_can_promote_to_pr(stored.to_dict())
        gov.close()

    asyncio.run(run_case())
