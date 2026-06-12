from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext, proposal


class PullRequestTool:
    """Create pull requests only. It never merges PRs and never enables auto-merge."""

    name = "pull_request"

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["gh", *args], cwd=self.repo_root, text=True, capture_output=True, timeout=60, check=False)

    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if action != "create":
            raise ValueError(f"Unsupported pull_request action: {action}")
        title = str(args["title"])
        body = str(args.get("body", ""))
        base = str(args.get("base", "main"))
        head = str(args.get("head", ""))
        draft = bool(args.get("draft", True))

        if not head.startswith("ai/"):
            return ToolResult(False, error="AI-created PR head branch must start with ai/")

        ctx.permissions.verify(proposal(
            "pull_request", "create",
            {"title": title, "base": base, "head": head, "draft": draft, "body_preview": body[:300]},
            "high", "创建 PR，但不自动合并", ctx
        ))

        cmd = ["pr", "create", "--title", title, "--body", body, "--base", base, "--head", head]
        if draft:
            cmd.append("--draft")
        r = self._run(cmd)
        ok = r.returncode == 0
        return ToolResult(ok, data={"stdout": r.stdout, "stderr": r.stderr, "exit_code": r.returncode}, summary=r.stdout.strip() or r.stderr.strip(), error=None if ok else r.stderr)
