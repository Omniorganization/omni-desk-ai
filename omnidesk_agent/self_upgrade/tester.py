from __future__ import annotations

from pathlib import Path

from omnidesk_agent.self_upgrade.models import TestResult
from omnidesk_agent.self_upgrade.sandbox_runner import SandboxRunner


class UpgradeTester:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()
        self.runner = SandboxRunner(self.repo_root)

    async def run(self, command: str, timeout: int = 120) -> TestResult:
        return await self.runner.run(command, timeout=timeout)
