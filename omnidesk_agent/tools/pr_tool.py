from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, cast

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext, proposal
from omnidesk_agent.tools.github_preflight import GitHubPreflight, sanitize_command_output


class PullRequestTool:
    """Create pull requests only. It never merges PRs and never enables auto-merge."""

    name = "pull_request"

    def __init__(self, repo_root: Path, *, remote_name: str = "origin", host: str = "github.com", require_write_access: bool = True):
        self.repo_root = repo_root.resolve()
        self.remote_name = remote_name
        self.host = host
        self.require_write_access = require_write_access

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["gh", *args], cwd=self.repo_root, text=True, capture_output=True, timeout=60, check=False)

    def _preflight(self, *, head: str) -> dict[str, object]:
        return GitHubPreflight(
            self.repo_root,
            remote_name=self.remote_name,
            host=self.host,
        ).run(head=head, require_write=self.require_write_access).to_dict()

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

        preflight = self._preflight(head=head)
        if not preflight.get("ok"):
            errors = cast(list[object], preflight.get("errors") or [])
            summary = "; ".join(str(e) for e in errors) or "GitHub preflight failed"
            return ToolResult(False, data={"preflight": preflight}, summary=summary, error=summary)

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
        stdout = sanitize_command_output(r.stdout)
        stderr = sanitize_command_output(r.stderr)
        summary = stdout.strip() or stderr.strip()
        return ToolResult(ok, data={"stdout": stdout, "stderr": stderr, "exit_code": r.returncode, "preflight": preflight}, summary=summary, error=None if ok else stderr)
