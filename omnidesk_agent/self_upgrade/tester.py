from __future__ import annotations

import asyncio
from pathlib import Path

from omnidesk_agent.self_upgrade.models import TestResult


class UpgradeTester:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()

    async def run(self, command: str, timeout: int = 120) -> TestResult:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self.repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return TestResult(False, command, f"Timed out after {timeout}s", 124)
        text = output.decode(errors="replace")[-12000:]
        return TestResult(proc.returncode == 0, command, text, int(proc.returncode or 0))
