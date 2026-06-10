from __future__ import annotations

from pathlib import Path
from omnidesk_agent.self_upgrade.sandbox_runner import SandboxRunner


class RegressionRunner:
    REQUIRED = [
        "tests/regression/test_self_upgrade_governance.py",
    ]

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()
        self.runner = SandboxRunner(self.repo_root)

    async def run(self, target: str = "tests/regression") -> dict:
        missing = [p for p in self.REQUIRED if not (self.repo_root / p).exists()]
        if missing:
            return {"ok": False, "skipped": True, "reason": "required regression tests missing", "missing": missing}
        if not (self.repo_root / target).exists():
            return {"ok": False, "skipped": True, "reason": f"{target} does not exist"}
        result = await self.runner.run(["pytest", target], timeout=180)
        return {"ok": result.ok, "skipped": False, "command": result.command, "exit_code": result.exit_code, "output": result.output}
