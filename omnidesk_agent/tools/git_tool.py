from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path
from typing import Any

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.sandbox.isolation import minimal_host_env
from omnidesk_agent.security.command_policy import harden_exec_argv
from omnidesk_agent.tools.base import ToolContext, proposal, permission_guarded

# An agent branch is a plain name under ai/: no refspec syntax (':' '+'), no
# ref paths, no option-looking names, nothing git check-ref-format would refuse.
_AI_BRANCH_RE = re.compile(r"ai/[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*")


def _valid_ai_branch(branch: str) -> bool:
    return bool(_AI_BRANCH_RE.fullmatch(branch)) and ".." not in branch and not branch.endswith((".lock", ".")) and len(branch) <= 200


class GitTool:
    """Restricted Git operations for Level 3 self-upgrade.

    Deliberately excluded: merge, rebase, reset --hard, force-push, deleting
    branches, changing remotes, and restarting services.
    """

    name = "git"

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()

    def _run(self, args: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
        argv = ["git", *args]
        return subprocess.run(
            # Same execution hardening as ShellTool: repository config cannot run
            # hooks, fsmonitor, external diff or editors, and git sees a minimal
            # environment instead of the gateway's secrets.
            harden_exec_argv(argv),
            cwd=self.repo_root,
            env=minimal_host_env(argv),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    @permission_guarded
    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        allowed = {"status", "diff", "checkout_new_branch", "add", "commit", "push"}
        if action not in allowed:
            return ToolResult(False, error=f"Unsupported git action: {action}")

        risk = "low" if action in {"status", "diff"} else "high"
        decision = ctx.permissions.verify(proposal("git", action, args, risk, f"执行受控 Git 操作：{action}", ctx))
        if decision.mode == "dry_run":
            return ToolResult(False, summary=f"dry-run git {action}")

        if action == "status":
            result = await asyncio.to_thread(self._run, ["status", "--short"])
        elif action == "diff":
            result = await asyncio.to_thread(self._run, ["diff", "--", "."])
        elif action == "checkout_new_branch":
            branch = str(args["branch"])
            if not _valid_ai_branch(branch):
                return ToolResult(False, error="AI upgrade branches must be plain names under ai/")
            result = await asyncio.to_thread(self._run, ["checkout", "-b", branch])
        elif action == "add":
            result = await asyncio.to_thread(self._run, ["add", "."])
        elif action == "commit":
            message = str(args.get("message") or "AI upgrade proposal")
            result = await asyncio.to_thread(self._run, ["commit", "-m", message])
        elif action == "push":
            branch = str(args["branch"])
            if not _valid_ai_branch(branch):
                # 'ai/x:main' is a refspec that pushes to main (2026-11-21 audit probe).
                return ToolResult(False, error="Only plain ai/* branches can be pushed by the agent")
            result = await asyncio.to_thread(self._run, ["push", "-u", "origin", branch])
        else:  # pragma: no cover
            return ToolResult(False, error=f"Unhandled git action: {action}")

        output = (result.stdout + result.stderr)[-12000:]
        return ToolResult(
            ok=result.returncode == 0,
            data={"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode},
            summary=output,
            error=None if result.returncode == 0 else output,
        )
