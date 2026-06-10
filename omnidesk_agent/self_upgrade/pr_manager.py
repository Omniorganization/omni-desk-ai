from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class PRPreparationResult:
    ok: bool
    branch: str
    stdout: str = ""
    stderr: str = ""


class SelfUpgradePRManager:
    """PR-only self-upgrade guard.

    This class refuses to apply self-upgrade changes directly to `main`. It can
    prepare an `ai/...` branch and returns evidence for a human-created or
    `gh pr create`-created PR. It never merges and never restarts services.
    """

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()

    def current_branch(self) -> str:
        return self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    def ensure_not_main(self) -> None:
        branch = self.current_branch()
        if branch in {"main", "master"}:
            raise PermissionError("self-upgrade patching must happen on an ai/* branch, never directly on main/master")
        if not branch.startswith("ai/"):
            raise PermissionError("self-upgrade branch must start with ai/")

    def prepare_branch(self, branch: str, base: str = "main") -> PRPreparationResult:
        if not branch.startswith("ai/"):
            raise PermissionError("self-upgrade branch must start with ai/")
        r = self._run(["git", "checkout", "-B", branch, base], check=False)
        return PRPreparationResult(ok=r.returncode == 0, branch=branch, stdout=r.stdout, stderr=r.stderr)

    def diff_evidence(self) -> str:
        return self._run(["git", "diff", "--stat"]).stdout + "\n" + self._run(["git", "diff", "--check"], check=False).stdout

    def _run(self, argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(argv, cwd=self.repo_root, text=True, capture_output=True, timeout=60)
        if check and result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout)
        return result
