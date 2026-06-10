from __future__ import annotations
from pathlib import Path
from omnidesk_agent.self_upgrade.sandbox_runner import SandboxRunner
class SecurityTestRunner:
    def __init__(self, repo_root: Path): self.repo_root = repo_root.resolve(); self.runner = SandboxRunner(self.repo_root)
    async def run(self) -> dict:
        candidates = ["tests/test_upgrade_gate.py", "tests/test_permission_remote_approval.py", "tests/test_resume_token_and_approval_scope.py", "tests/test_webhook_security.py", "tests/test_permission_diff_checker.py"]
        existing = [p for p in candidates if (self.repo_root / p).exists()]
        if not existing: return {"ok": True, "skipped": True, "reason": "no security regression tests found"}
        result = await self.runner.run(["pytest"] + existing, timeout=180)
        return {"ok": result.ok, "skipped": False, "command": result.command, "exit_code": result.exit_code, "output": result.output}
