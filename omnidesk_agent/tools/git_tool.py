from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext, proposal


class GitTool:
    """Restricted Git operations for Level 3 self-upgrade.

    Deliberately excluded: merge, rebase, reset --hard, force-push, deleting
    branches, changing remotes, and restarting services.
    """

    name = "git"

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()

    def _run(self, args: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        allowed = {"status", "diff", "checkout_new_branch", "add", "commit", "push"}
        if action not in allowed:
            return ToolResult(False, error=f"Unsupported git action: {action}")

        risk = "low" if action in {"status", "diff"} else "high"
        decision = ctx.permissions.verify(proposal("git", action, args, risk, f"执行受控 Git 操作：{action}", ctx))
        if decision.mode == "dry_run":
            return ToolResult(False, summary=f"dry-run git {action}")

        if action == "status":
            result = self._run(["status", "--short"])
        elif action == "diff":
            result = self._run(["diff", "--", "."])
        elif action == "checkout_new_branch":
            branch = str(args["branch"])
            if not branch.startswith("ai/"):
                return ToolResult(False, error="AI upgrade branches must start with ai/")
            result = self._run(["checkout", "-b", branch])
        elif action == "add":
            result = self._run(["add", "."])
        elif action == "commit":
            message = str(args.get("message") or "AI upgrade proposal")
            result = self._run(["commit", "-m", message])
        elif action == "push":
            branch = str(args["branch"])
            if not branch.startswith("ai/"):
                return ToolResult(False, error="Only ai/* branches can be pushed by the agent")
            result = self._run(["push", "-u", "origin", branch])
        else:  # pragma: no cover
            return ToolResult(False, error=f"Unhandled git action: {action}")

        output = (result.stdout + result.stderr)[-12000:]
        return ToolResult(
            ok=result.returncode == 0,
            data={"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode},
            summary=output,
            error=None if result.returncode == 0 else output,
        )
