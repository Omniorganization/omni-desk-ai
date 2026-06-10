from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Snapshot:
    commit: str
    branch: str


class RollbackManager:
    """Git-based snapshot and rollback helper."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()

    def snapshot_current_version(self) -> Snapshot:
        commit = self._run(["git", "rev-parse", "HEAD"]).strip()
        branch = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).strip()
        return Snapshot(commit=commit, branch=branch)

    def health_check(self) -> bool:
        result = subprocess.run(["python3", "-m", "compileall", "omnidesk_agent"], cwd=self.repo_root, text=True, capture_output=True)
        return result.returncode == 0

    def rollback(self, snapshot: Snapshot) -> str:
        self._run(["git", "reset", "--hard", snapshot.commit])
        return f"rolled back to {snapshot.commit} on {snapshot.branch}"

    def _run(self, argv: list[str]) -> str:
        result = subprocess.run(argv, cwd=self.repo_root, text=True, capture_output=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout)
        return result.stdout
