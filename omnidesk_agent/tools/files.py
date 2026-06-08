from __future__ import annotations

from pathlib import Path
from typing import Any

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext, proposal


class FilesTool:
    name = "files"

    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root.resolve()

    def _safe_path(self, path: str) -> Path:
        p = (self.workspace_root / path).expanduser().resolve() if not Path(path).is_absolute() else Path(path).expanduser().resolve()
        if not str(p).startswith(str(self.workspace_root)):
            raise ValueError(f"Path outside workspace: {p}")
        return p

    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if action == "read_text":
            p = self._safe_path(str(args["path"]))
            ctx.permissions.verify(proposal("files", "read_text", {"path": str(p)}, "low", "读取工作区文件", ctx))
            return ToolResult(True, data=p.read_text(encoding=args.get("encoding", "utf-8")), summary=f"read {p}")
        if action == "write_text":
            p = self._safe_path(str(args["path"]))
            text = str(args.get("text", ""))
            decision = ctx.permissions.verify(proposal(
                "files", "write_text", {"path": str(p), "bytes": len(text.encode())}, "high", "写入工作区文件", ctx
            ))
            if decision.mode == "dry_run":
                return ToolResult(False, summary=f"dry-run: write {p}")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding=args.get("encoding", "utf-8"))
            return ToolResult(True, summary=f"wrote {p}")
        if action == "list":
            p = self._safe_path(str(args.get("path", ".")))
            ctx.permissions.verify(proposal("files", "list", {"path": str(p)}, "low", "列出工作区文件", ctx))
            return ToolResult(True, data=[str(x.relative_to(self.workspace_root)) for x in p.iterdir()], summary=f"listed {p}")
        raise ValueError(f"Unsupported files action: {action}")
