from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol
import hashlib
import json

from omnidesk_agent.core.models import ActionProposal, ToolResult
from omnidesk_agent.security.permissions import PermissionManager


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


def proposal(tool: str, action: str, args: dict[str, Any], risk: str, reason: str, ctx: ToolContext) -> ActionProposal:
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
