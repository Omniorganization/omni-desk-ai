from __future__ import annotations

from dataclasses import dataclass, replace
from functools import wraps
from typing import Any, Awaitable, Callable, Optional, Protocol
import hashlib
import json

from omnidesk_agent.core.models import ActionProposal, RiskLevel, ToolResult
from omnidesk_agent.security.permissions import PermissionDecision, PermissionManager


@dataclass
class ToolContext:
    permissions: PermissionManager
    source: str = "local-cli"
    actor: str = "owner"
    run_id: Optional[str] = None
    plan_id: Optional[str] = None
    step_index: Optional[int] = None


class Tool(Protocol):
    name: str

    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        ...


class _DryRun(Exception):
    pass


class PolicyRefusal(PermissionError):
    """A tool refused a request by its own policy, before any approval: `rule` names the check."""

    def __init__(self, message: str, *, rule: str):
        super().__init__(message)
        self.rule = rule


_AUDITED = "_omnidesk_permission_audited"


class _ExecutionPermissions(PermissionManager):
    """Call-local adapter: a returned denial must stop the protected operation."""

    def __init__(self, delegate: PermissionManager):
        self._delegate = delegate

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def verify(self, action_proposal: Any) -> PermissionDecision:
        try:
            decision = self._delegate.verify(action_proposal)
        except PermissionError as exc:
            # The permission manager audited this denial itself.
            setattr(exc, _AUDITED, True)
            raise
        if getattr(decision, "mode", None) == "dry_run":
            raise _DryRun()
        if getattr(decision, "allowed", False) is not True:
            refused = PermissionError("Permission policy did not authorize execution")
            setattr(refused, _AUDITED, True)
            raise refused
        return decision


def record_access(ctx: ToolContext, action_proposal: ActionProposal) -> None:
    """Audit an action that runs without an approval gate (files.list).

    The permission manager raises when its audit log cannot be written; the
    action must then not run.
    """
    record = getattr(ctx.permissions, "record_access", None)
    if callable(record):
        try:
            record(action_proposal)
        except PermissionError as exc:
            setattr(exc, _AUDITED, True)  # the audit itself failed; nothing more to record
            raise


def _audit_policy_refusal(tool: str, action: str, args: dict[str, Any], ctx: ToolContext, exc: PermissionError) -> None:
    record = getattr(ctx.permissions, "record_policy_block", None)
    if not callable(record):
        return
    rule = str(getattr(exc, "rule", "") or "tool_policy")
    record(proposal(tool, action, dict(args or {}), "low", "refused by tool policy", ctx), rule=rule, reason=str(exc)[:500])


def permission_guarded(call: Callable[..., Awaitable[ToolResult]]) -> Callable[..., Awaitable[ToolResult]]:
    """Enforce decisions on direct calls as well as calls through the registry.

    A PermissionError that did not come from the permission manager is a refusal
    by the tool's own policy (a path outside the workspace, a disabled
    capability, ...); it is audited before it propagates.

    Do not mutate the shared PermissionManager or ToolContext: concurrent tool
    calls must not inherit a different call's execution wrapper.
    """
    @wraps(call)
    async def guarded(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        guarded_ctx = replace(ctx, permissions=_ExecutionPermissions(ctx.permissions))
        try:
            return await call(self, action, args, guarded_ctx)
        except _DryRun:
            return ToolResult(False, data={"dry_run": True}, summary=f"dry-run {self.name}.{action}; no execution authorized")
        except PermissionError as exc:
            if not getattr(exc, _AUDITED, False):
                _audit_policy_refusal(self.name, action, args, guarded_ctx, exc)
            raise

    return guarded


def _scope_hash(tool: str, action: str, args: dict[str, Any], ctx: ToolContext) -> str:
    safe_args = {
        k: ("[REDACTED]" if any(s in k.lower() for s in ("token", "secret", "password", "api_key", "authorization")) else v)
        for k, v in (args or {}).items()
    }
    payload = {
        "run_id": ctx.run_id,
        "plan_id": ctx.plan_id,
        "step_index": ctx.step_index,
        "tool": tool,
        "action": action,
        "args": safe_args,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def content_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def json_sha256(value: Any) -> str:
    """Digest of a JSON-serialisable value in the canonical form the scope hash uses."""
    return content_sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str))


def bound_content(name: str, value: str) -> dict[str, Any]:
    """Proposal fields that show the approver, and bind into the scope hash,
    every character of a side effect's content.

    Never put a preview (``value[:N]``) in a proposal instead: the approver
    would not see the rest, and the scope hash would accept any content with
    the same prefix, so a payload hidden past the cut rides on the approval.
    """
    return {name: value, f"{name}_sha256": content_sha256(value), f"{name}_length": len(value)}


def proposal(tool: str, action: str, args: dict[str, Any], risk: RiskLevel, reason: str, ctx: ToolContext) -> ActionProposal:
    return ActionProposal(
        tool=tool,
        action=action,
        args=args,
        risk=risk,
        reason=reason,
        source=ctx.source,
        actor=ctx.actor,
        run_id=ctx.run_id,
        plan_id=ctx.plan_id,
        step_index=ctx.step_index,
        scope_hash=_scope_hash(tool, action, args, ctx),
    )
