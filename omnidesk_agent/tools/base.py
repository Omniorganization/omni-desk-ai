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


class _ExecutionPermissions(PermissionManager):
    """Call-local adapter: a returned denial must stop the protected operation."""

    def __init__(self, delegate: PermissionManager):
        self._delegate = delegate

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def verify(self, action_proposal: Any) -> PermissionDecision:
        decision = self._delegate.verify(action_proposal)
        if getattr(decision, "mode", None) == "dry_run":
            raise _DryRun()
        if getattr(decision, "allowed", False) is not True:
            raise PermissionError("Permission policy did not authorize execution")
        return decision


def permission_guarded(call: Callable[..., Awaitable[ToolResult]]) -> Callable[..., Awaitable[ToolResult]]:
    """Enforce decisions on direct calls as well as calls through the registry.

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
