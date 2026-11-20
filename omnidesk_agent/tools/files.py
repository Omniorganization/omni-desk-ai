from __future__ import annotations

from pathlib import Path
from typing import Any

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import PolicyRefusal, ToolContext, bound_content, permission_guarded, proposal, record_access
from omnidesk_agent.tools.spec import ActionSpec, ToolSpec


# Runtime state that lives under the workspace root: the run, approval, memory and
# token stores and their journals, and audit files. An agent that could read them
# would see other runs and actors; one that could write them could approve itself.
_SECURITY_STATE_SUFFIXES = (".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".sqlite3-journal")
_SECURITY_STATE_NAMES = frozenset({"admin_auth_audit.jsonl", "audit_checkpoints"})


def _is_security_state(relative: Path) -> bool:
    return any(part.lower().endswith(_SECURITY_STATE_SUFFIXES) or part.lower() in _SECURITY_STATE_NAMES for part in relative.parts)


class FilesTool:
    name = "files"

    def __init__(self, root: Path, *, allow_write: bool = False):
        self.root = root.expanduser().resolve()
        self.allow_write = allow_write
        self.root.mkdir(parents=True, exist_ok=True)

    def spec(self) -> ToolSpec:
        spec = ToolSpec(
            name=self.name,
            description="Read and write files inside the Omni-desk workspace.",
            permissions=["files.read", "files.write"],
            actions={
                "read_text": ActionSpec("read_text", "Read a UTF-8 text file from workspace", {"path": "string"}, risk="medium", side_effect=False, requires_approval=True),
                "write_text": ActionSpec("write_text", "Write a UTF-8 text file inside workspace", {"path": "string", "text": "string"}, risk="high", side_effect=True, requires_approval=True),
                "list": ActionSpec("list", "List files under a workspace directory", {"path": "string"}, risk="low", side_effect=False, requires_approval=False),
            },
        )
        if not self.allow_write:
            spec.permissions.remove("files.write")
            spec.actions.pop("write_text")
        return spec

    def _safe_path(self, rel: str) -> Path:
        raw = Path(rel).expanduser()
        if raw.is_absolute():
            candidate = raw.resolve()
        else:
            candidate = (self.root / raw).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise PolicyRefusal(f"path escapes workspace: {rel}", rule="path_escape") from exc
        return candidate

    def _content_path(self, rel: str) -> Path:
        """A workspace path whose content the agent may read or write."""
        path = self._safe_path(rel)
        if _is_security_state(path.relative_to(self.root)):
            raise PolicyRefusal(f"runtime security state is not accessible to the agent: {rel}", rule="security_state")
        return path

    def _writable_path(self, rel: str) -> Path:
        path = self._content_path(rel)
        # Allowlisted git commands run in this same root. Repository config,
        # hooks and nested .git dirs/files decide what git executes (fsmonitor,
        # diff/filter drivers, sshCommand, ...), so the agent must never write them.
        if any(part.lower() == ".git" for part in path.relative_to(self.root).parts):
            raise PolicyRefusal(f"writes to git metadata are not allowed: {rel}", rule="git_metadata_write")
        return path

    @permission_guarded
    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if action == "read_text":
            path = self._content_path(str(args["path"]))
            ctx.permissions.verify(proposal("files", "read_text", {"path": str(path)}, "medium", "读取工作区文件", ctx))
            text = path.read_text(encoding="utf-8")
            if len(text) > 20000:
                text = text[:10000] + "\n...[TRUNCATED]...\n" + text[-10000:]
            return ToolResult(True, data={"text": text, "path": str(path)}, summary=f"read {path.name}")

        if action == "write_text":
            if not self.allow_write:
                raise PolicyRefusal("File writes are disabled by capability policy", rule="capability_disabled")
            path = self._writable_path(str(args["path"]))
            text = str(args.get("text", ""))
            expected = str(args.get("expected_result") or f"Write {path.name}")
            # The approver sees, and the scope hash binds, the whole file content.
            approved: dict[str, Any] = {"path": str(path), **bound_content("text", text), "expected_result": expected}
            ctx.permissions.verify(proposal("files", "write_text", approved, "high", "写入工作区文件", ctx))
            path.parent.mkdir(parents=True, exist_ok=True)
            # Bytes, not write_text: no newline translation, so the file holds
            # exactly the approved characters on every platform.
            data = text.encode("utf-8")
            path.write_bytes(data)
            return ToolResult(True, data={"path": str(path), "bytes": len(data)}, summary=f"wrote {path}")

        if action == "list":
            path = self._safe_path(str(args.get("path", ".")))
            # Listing is ungated (names only, low risk) but audited; no audit, no listing.
            record_access(ctx, proposal("files", "list", {"path": str(path)}, "low", "列出工作区目录", ctx))
            items = [{"name": p.name, "is_dir": p.is_dir(), "size": p.stat().st_size if p.is_file() else None} for p in path.iterdir()]
            return ToolResult(True, data={"items": items, "path": str(path)}, summary=f"listed {len(items)} items")

        raise ValueError(f"Unsupported files action: {action}")
