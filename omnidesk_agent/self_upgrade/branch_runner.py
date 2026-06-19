from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BranchRunResult:
    ok: bool
    branch: str
    base: str
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BranchRunner:
    """Create governed ai/* branches for repair work; never merge them."""

    BRANCH_RE = re.compile(r"^ai/[a-z0-9][a-z0-9._/-]{0,96}$")

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()

    def prepare(self, branch: str, *, base: str = "main") -> BranchRunResult:
        if not self.BRANCH_RE.match(branch):
            raise PermissionError("repair branches must match ai/<slug>")
        result = subprocess.run(
            ["git", "checkout", "-B", branch, base],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            timeout=60,
        )
        return BranchRunResult(result.returncode == 0, branch, base, result.stdout, result.stderr)
