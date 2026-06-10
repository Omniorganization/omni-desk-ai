from __future__ import annotations

from pathlib import Path
from omnidesk_agent.self_upgrade.sandbox_runner import SandboxRunner


class SecurityTestRunner:
    REQUIRED = [
        "tests/test_upgrade_gate.py",
        "tests/test_permission_diff_checker.py",
        "tests/test_admin_auth.py",
        "tests/test_permission_session_scope.py",
    ]

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()
        self.runner = SandboxRunner(self.repo_root)

    async def run(self) -> dict:
        missing = [p for p in self.REQUIRED if not (self.repo_root / p).exists()]
        if missing:
            return {"ok": False, "skipped": True, "reason": "required security tests missing", "missing": missing}
        result = await self.runner.run(["pytest"] + self.REQUIRED, timeout=180)
        return {"ok": result.ok, "skipped": False, "command": result.command, "exit_code": result.exit_code, "output": result.output}
