from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Awaitable, Protocol

from omnidesk_agent.core.models import ActionProposal, ToolResult
from omnidesk_agent.security.permissions import PermissionManager


@dataclass(slots=True)
class ToolContext:
    permissions: PermissionManager
    source: str = "local-cli"
    actor: str = "owner"


class Tool(Protocol):
    name: str

    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


def proposal(tool: str, action: str, args: dict[str, Any], risk: str, reason: str, ctx: ToolContext) -> ActionProposal:
    return ActionProposal(tool=tool, action=action, args=args, risk=risk, reason=reason, source=ctx.source, actor=ctx.actor)
