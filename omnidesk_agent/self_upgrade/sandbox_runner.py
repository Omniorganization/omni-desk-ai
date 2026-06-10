from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

from omnidesk_agent.self_upgrade.models import TestResult


class SandboxRunner:
    """Safe-ish command runner for upgrade tests.

    Uses argv execution and allowlisted prefixes. It does not use shell=True.
    """

    DEFAULT_ALLOWED = [
        ["python", "-m", "compileall"],
        ["python3", "-m", "compileall"],
        ["pytest"],
        ["ruff", "check"],
        ["git", "diff"],
        ["git", "status"],
    ]

    def __init__(self, repo_root: Path, allowed_prefixes: list[list[str]] | None = None):
        self.repo_root = repo_root.resolve()
        self.allowed_prefixes = allowed_prefixes or self.DEFAULT_ALLOWED

    def allowed(self, argv: list[str]) -> bool:
        return any(len(argv) >= len(prefix) and argv[:len(prefix)] == prefix for prefix in self.allowed_prefixes)

    async def run(self, command: str | list[str], timeout: int = 120) -> TestResult:
        argv = [str(x) for x in command] if isinstance(command, list) else shlex.split(command)
        if not argv:
            return TestResult(False, str(command), "empty command", 2)
        if not self.allowed(argv):
            return TestResult(False, " ".join(argv), f"blocked by sandbox allowlist: {argv}", 126)

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self.repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return TestResult(False, " ".join(argv), f"Timed out after {timeout}s", 124)
        text = output.decode(errors="replace")[-12000:]
        return TestResult(proc.returncode == 0, " ".join(argv), text, int(proc.returncode or 0))
