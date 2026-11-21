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


# What the execution wrapper answers itself; everything else is the wrapped manager's.
_WRAPPER_OWN_ATTRIBUTES = frozenset({"verify", "_delegate", "__class__", "__dict__"})


class _ExecutionPermissions(PermissionManager):
    """Call-local adapter: a returned denial must stop the protected operation.

    It subclasses PermissionManager for its type only. Every attribute except
    verify, methods included, is the wrapped manager's: an inherited method
    run with this throwaway object as self would write its state here (an
    audit-integrity failure flag, a derived key) instead of on the manager,
    which then would keep accepting decisions.
    """

    def __init__(self, delegate: PermissionManager):
        self._delegate = delegate

    def __getattribute__(self, name: str) -> Any:
        if name in _WRAPPER_OWN_ATTRIBUTES:
            return object.__getattribute__(self, name)
        return getattr(object.__getattribute__(self, "_delegate"), name)

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


def _digest(data: bytes, ctx: Optional[Any]) -> str:
    """HMAC-SHA256 under the deployment's digest key when the permission manager provides one, else SHA-256.

    A plain SHA-256 of low-entropy content (a typed password) lets anyone who can
    read approval records or the audit log, where the content itself is withheld,
    test guesses offline. ``PermissionManager.keyed_digest(data)`` keys it with a key
    derived from the audit key, stable across restarts and replicas.
    """
    # Through the execution wrapper this is the manager's own bound method (see _ExecutionPermissions).
    keyed = getattr(getattr(ctx, "permissions", None), "keyed_digest", None) if ctx is not None else None
    if callable(keyed):
        value = keyed(data)
        if isinstance(value, str) and value:
            return value
    return hashlib.sha256(data).hexdigest()


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
    return _digest(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8"), ctx)


def content_sha256(value: str, ctx: Optional[Any] = None) -> str:
    """Digest binding `value` into an approval; keyed when `ctx`'s permission manager has a digest key."""
    return _digest(value.encode("utf-8"), ctx)


def json_sha256(value: Any, ctx: Optional[Any] = None) -> str:
    """Digest of a JSON-serialisable value in the canonical form the scope hash uses."""
    return content_sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str), ctx)


def bound_content(name: str, value: str, ctx: Optional[Any] = None) -> dict[str, Any]:
    """Proposal fields that show the approver, and bind into the scope hash,
    every character of a side effect's content.

    Never put a preview (``value[:N]``) in a proposal instead: the approver
    would not see the rest, and the scope hash would accept any content with
    the same prefix, so a payload hidden past the cut rides on the approval.
    """
    return {name: value, f"{name}_sha256": content_sha256(value, ctx), f"{name}_length": len(value)}


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
