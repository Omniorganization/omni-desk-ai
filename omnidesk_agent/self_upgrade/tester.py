from __future__ import annotations

from pathlib import Path
from omnidesk_agent.config import SandboxConfig

from omnidesk_agent.self_upgrade.models import TestResult
from omnidesk_agent.self_upgrade.sandbox_runner import SandboxRunner


class UpgradeTester:
    def __init__(self, repo_root: Path, sandbox_cfg: SandboxConfig | None = None, *, require_isolation: bool = False):
        self.repo_root = repo_root.resolve()
        self.runner = SandboxRunner(self.repo_root, sandbox_cfg=sandbox_cfg, require_isolation=require_isolation)

    async def run(self, command: str, timeout: int = 120) -> TestResult:
        return await self.runner.run(command, timeout=timeout)
