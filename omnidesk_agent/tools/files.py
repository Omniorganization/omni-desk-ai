from __future__ import annotations

from pathlib import Path
from typing import Any

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext, proposal
from omnidesk_agent.tools.spec import ActionSpec, ToolSpec


class FilesTool:
    name = "files"

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description="Read and write files inside the Omni-desk workspace.",
            permissions=["files.read", "files.write"],
            actions={
                "read_text": ActionSpec("read_text", "Read a UTF-8 text file from workspace", {"path": "string"}, risk="medium", side_effect=False, requires_approval=True),
                "write_text": ActionSpec("write_text", "Write a UTF-8 text file inside workspace", {"path": "string", "text": "string"}, risk="high", side_effect=True, requires_approval=True),
                "list": ActionSpec("list", "List files under a workspace directory", {"path": "string"}, risk="low", side_effect=False, requires_approval=False),
            },
        )

    def _safe_path(self, rel: str) -> Path:
        p = (self.root / rel).expanduser().resolve()
        if not str(p).startswith(str(self.root)):
            raise PermissionError(f"path escapes workspace: {rel}")
        return p

    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if action == "read_text":
            path = self._safe_path(str(args["path"]))
            ctx.permissions.verify(proposal("files", "read_text", {"path": str(path)}, "medium", "读取工作区文件", ctx))
            text = path.read_text(encoding="utf-8")
            if len(text) > 20000:
                text = text[:10000] + "\n...[TRUNCATED]...\n" + text[-10000:]
            return ToolResult(True, data={"text": text, "path": str(path)}, summary=f"read {path.name}")

        if action == "write_text":
            path = self._safe_path(str(args["path"]))
            text = str(args.get("text", ""))
            expected = str(args.get("expected_result") or f"Write {path.name}")
            ctx.permissions.verify(proposal("files", "write_text", {"path": str(path), "length": len(text), "expected_result": expected}, "high", "写入工作区文件", ctx))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return ToolResult(True, data={"path": str(path), "bytes": len(text.encode("utf-8"))}, summary=f"wrote {path}")

        if action == "list":
            path = self._safe_path(str(args.get("path", ".")))
            items = [{"name": p.name, "is_dir": p.is_dir(), "size": p.stat().st_size if p.is_file() else None} for p in path.iterdir()]
            return ToolResult(True, data={"items": items, "path": str(path)}, summary=f"listed {len(items)} items")

        raise ValueError(f"Unsupported files action: {action}")
