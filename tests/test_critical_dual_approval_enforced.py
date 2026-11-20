"""Critical actions run only after two distinct owners approve them remotely.

Regression tests for the 2026-11-21 security audit (high): with
``permissions.default_mode=allow`` ``verify()`` allowed critical actions before
the remote/dual path was reached, a single console 'y' approved a critical
action, and the production validator never looked at ``default_mode``. Each
test below reproduces one of those bypasses and must fail if it comes back.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnidesk_agent.config import AppConfig, PermissionConfig
from omnidesk_agent.core.models import ChannelMessage, Plan, PlanStep, ToolResult
from omnidesk_agent.core.orchestrator import Orchestrator
from omnidesk_agent.core.run_store import RunStore
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.security.approval_required import ApprovalRequired
from omnidesk_agent.security.approval_store import ApprovalStore
from omnidesk_agent.security.dual_approval import DualApprovalStore
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.tools.base import ToolContext, proposal
from omnidesk_agent.tools.registry import ToolRegistry
from omnidesk_agent.validation.production import validate_production_config


def _critical(**extra):
    return {"tool": "shell", "action": "run", "args": {"argv": ["pytest"]}, "risk": "critical",
            "source": "local-cli", "actor": "requester", "scope_hash": "scope-1", **extra}


def _stores(tmp_path: Path) -> tuple[ApprovalStore, DualApprovalStore]:
    dual = DualApprovalStore(tmp_path / "dual.sqlite3")
    return ApprovalStore(tmp_path / "approvals.sqlite3", dual_approval_store=dual), dual


def _console_must_not_be_asked(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def _no_prompt(prompt: str = "") -> str:  # pragma: no cover - reached only on regression
        raise AssertionError("a critical action must never be approved from the console")

    monkeypatch.setattr("builtins.input", _no_prompt)


def test_default_allow_sends_critical_to_dual_remote_approval(tmp_path):
    approvals, dual = _stores(tmp_path)
    pm = PermissionManager(PermissionConfig(default_mode="allow", audit_log=tmp_path / "audit.log"), approvals)

    with pytest.raises(ApprovalRequired) as raised:
        pm.verify(_critical())

    assert raised.value.proposal["requires_dual_approval"] is True
    pending = approvals.get(raised.value.approval_id)
    assert pending["status"] == "pending"
    assert dual.status(raised.value.approval_id).reason == "waiting_for_second_approver"


def test_console_yes_cannot_approve_critical(tmp_path, monkeypatch):
    approvals, _dual = _stores(tmp_path)
    pm = PermissionManager(PermissionConfig(audit_log=tmp_path / "audit.log"), approvals)
    _console_must_not_be_asked(monkeypatch)

    with pytest.raises(ApprovalRequired) as raised:
        pm.verify(_critical())

    assert raised.value.proposal["requires_dual_approval"] is True


def test_critical_needs_dual_approval_even_if_config_drops_it(tmp_path):
    approvals, _dual = _stores(tmp_path)
    cfg = PermissionConfig(approval_mode="remote_approval", require_dual_approval_for_risks=[], audit_log=tmp_path / "audit.log")
    pm = PermissionManager(cfg, approvals)

    with pytest.raises(ApprovalRequired) as raised:
        pm.verify(_critical())

    approval_id = raised.value.approval_id
    approvals_without_dual = ApprovalStore(tmp_path / "approvals.sqlite3")
    # Even a store that lost its dual-approval wiring cannot approve it alone.
    with pytest.raises(PermissionError, match="dual approval store is required"):
        approvals_without_dual.decide(approval_id, "approved")


def test_critical_is_never_allowed_by_low_risk_session_or_auto_policy_shortcuts(tmp_path):
    approvals, _dual = _stores(tmp_path)
    cfg = PermissionConfig(
        approval_mode="auto_policy",
        always_ask_tools=[],
        require_dual_approval_for_risks=["critical", "low"],
        audit_log=tmp_path / "audit.log",
    )
    pm = PermissionManager(cfg, approvals)
    pm.session_allows.add(pm.session_key(_critical()))

    with pytest.raises(ApprovalRequired):
        pm.verify(_critical())
    # A risk configured for dual approval also skips the local-cli low-risk shortcut.
    with pytest.raises(ApprovalRequired):
        pm.verify(_critical(risk="low"))


def test_critical_without_approval_store_is_denied(tmp_path):
    pm = PermissionManager(PermissionConfig(default_mode="allow", audit_log=tmp_path / "audit.log"))

    with pytest.raises(PermissionError, match="ApprovalStore"):
        pm.verify(_critical())


def test_stricter_policies_still_win_for_critical(tmp_path, monkeypatch):
    approvals, _dual = _stores(tmp_path)

    deny = PermissionManager(PermissionConfig(default_mode="deny", audit_log=tmp_path / "deny.log"), approvals)
    with pytest.raises(PermissionError, match="Denied by default"):
        deny.verify(_critical())

    dry = PermissionManager(PermissionConfig(default_mode="dry_run", audit_log=tmp_path / "dry.log"), approvals)
    assert dry.verify(_critical()).mode == "dry_run"

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    no_tty = PermissionManager(PermissionConfig(audit_log=tmp_path / "notty.log"), approvals)
    with pytest.raises(PermissionError, match="no TTY"):
        no_tty.verify(_critical())
    assert approvals.list() == []


def test_resume_grant_runs_the_approved_scope_once(tmp_path):
    approvals, _dual = _stores(tmp_path)
    pm = PermissionManager(PermissionConfig(approval_mode="remote_approval", audit_log=tmp_path / "audit.log"), approvals)

    pm.allow_approved_proposal(_critical())

    assert pm.verify(_critical()).allowed is True
    # One dual approval is one execution: the same scope needs a new approval.
    with pytest.raises(ApprovalRequired):
        pm.verify(_critical())


def test_resume_grant_expires_with_the_approval_ttl(tmp_path, monkeypatch):
    approvals, _dual = _stores(tmp_path)
    cfg = PermissionConfig(approval_mode="remote_approval", approval_ttl_seconds=5, audit_log=tmp_path / "audit.log")
    pm = PermissionManager(cfg, approvals)
    clock = [1000.0]
    monkeypatch.setattr("omnidesk_agent.security.permissions.time.monotonic", lambda: clock[0])

    pm.allow_approved_proposal(_critical())
    clock[0] += 6

    with pytest.raises(ApprovalRequired):
        pm.verify(_critical())


class _CriticalPlanner:
    async def plan(self, msg):
        return Plan(goal="deploy", steps=[PlanStep("deploy", "deploy", "run", {"target": "prod"}, "critical", True)], plan_id="plan-1")


class _CriticalTool:
    name = "deploy"

    def __init__(self) -> None:
        self.executions = 0

    async def call(self, action: str, args: dict, ctx: ToolContext) -> ToolResult:
        ctx.permissions.verify(proposal("deploy", action, args, "critical", "critical test action", ctx))
        self.executions += 1
        return ToolResult(True, data={"executed": True}, summary="executed")


@pytest.mark.parametrize("console_yes", [False, True])
def test_runtime_with_bypass_config_waits_for_two_owners(tmp_path, monkeypatch, console_yes):
    """The audited configuration (interactive_cli + default_mode=allow, or a console 'y')."""
    if console_yes:
        _console_must_not_be_asked(monkeypatch)
        cfg = PermissionConfig(audit_log=tmp_path / "audit.log")
    else:
        cfg = PermissionConfig(default_mode="allow", audit_log=tmp_path / "audit.log")

    async def run_case() -> None:
        approvals, dual = _stores(tmp_path)
        permissions = PermissionManager(cfg, approvals)
        tool = _CriticalTool()
        tools = ToolRegistry()
        tools.register(tool)
        memory = ExperienceStore(tmp_path / "memory.sqlite3")
        runs = RunStore(tmp_path / "runs.sqlite3")
        orchestrator = Orchestrator(_CriticalPlanner(), tools, permissions, memory, run_store=runs,
                                    approval_store=approvals, dual_approval_store=dual)
        try:
            first = await orchestrator.handle_message(ChannelMessage(channel="test", sender_id="requester", text="deploy"))
            assert first["status"] == "waiting_approval"
            assert tool.executions == 0
            approval_id = first["approval_id"]

            with pytest.raises(PermissionError, match="dual approval is not satisfied"):
                approvals.decide(approval_id, "approved")
            with pytest.raises(PermissionError, match="creator cannot approve"):
                dual.approve(approval_id, "requester")
            dual.approve(approval_id, "owner-a")
            with pytest.raises(PermissionError, match="distinct"):
                dual.approve(approval_id, "owner-a")
            dual.approve(approval_id, "owner-b")
            approvals.decide(approval_id, "approved")

            resumed = await orchestrator.resume(first["run_id"], resume_token=first["resume_token"])
            assert resumed["status"] == "completed"
            assert tool.executions == 1
        finally:
            memory.close()

    asyncio.run(run_case())


def test_production_validator_rejects_default_allow(tmp_path):
    cfg = AppConfig()
    cfg.permissions.default_mode = "allow"

    issues = validate_production_config(cfg, {"OMNIDESK_ENV": "production"})["issues"]

    assert any("permissions.default_mode" in issue for issue in issues)

    cfg.permissions.default_mode = "ask"
    issues = validate_production_config(cfg, {"OMNIDESK_ENV": "production"})["issues"]
    assert not any("permissions.default_mode" in issue for issue in issues)


@pytest.mark.parametrize("mode,ok", [("interactive_cli", False), ("auto_policy", False), ("remote_approval", True)])
def test_production_validator_requires_remote_approval(mode, ok):
    cfg = AppConfig()
    cfg.permissions.approval_mode = mode

    issues = validate_production_config(cfg, {"OMNIDESK_ENV": "production"})["issues"]

    assert ("permissions.approval_mode must be remote_approval in production" not in issues) is ok
