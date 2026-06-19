from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class Snapshot:
    commit: str
    branch: str


@dataclass
class RollbackResult:
    rolled_back: bool
    health_ok: bool
    message: str


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

    def rollback_if_unhealthy(
        self,
        snapshot: Snapshot,
        *,
        upgrade_id: str,
        memory: Any | None = None,
        health_check: Callable[[], bool] | None = None,
    ) -> RollbackResult:
        check = health_check or self.health_check
        health_ok = bool(check())
        if health_ok:
            return RollbackResult(rolled_back=False, health_ok=True, message="health check passed; rollback not required")
        message = self.rollback(snapshot)
        if memory is not None:
            memory.record({
                "upgrade_id": upgrade_id,
                "change_type": "rollback",
                "target": snapshot.branch,
                "rollback": True,
                "verdict": "rolled_back",
                "metadata": {
                    "snapshot_commit": snapshot.commit,
                    "snapshot_branch": snapshot.branch,
                    "health_ok": False,
                    "message": message,
                },
            })
        return RollbackResult(rolled_back=True, health_ok=False, message=message)

    def _run(self, argv: list[str]) -> str:
        result = subprocess.run(argv, cwd=self.repo_root, text=True, capture_output=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout)
        return result.stdout
