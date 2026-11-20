"""Apply a checked diff in a throwaway git worktree and run commands there.

Commands get a minimal environment (no tokens, keys or gateway settings), so
model-written code under test cannot read the pipeline's credentials. In CI
this runs in a job that has no secrets and a read-only token at most.
"""
from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "PYTHONHASHSEED")


@dataclass
class CommandResult:
    argv: list[str]
    exit_code: int
    seconds: float
    output_tail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IsolatedWorktree:
    def __init__(self, repo_root: Path, base_ref: str = "HEAD"):
        self.repo_root = Path(repo_root).resolve()
        self.base_ref = base_ref
        self.path: Optional[Path] = None
        self.base_sha = ""

    def __enter__(self) -> "IsolatedWorktree":
        self.path = Path(tempfile.mkdtemp(prefix="omnidesk-upgrade-")) / "tree"
        self.base_sha = self._git(self.repo_root, "rev-parse", "--verify", f"{self.base_ref}^{{commit}}").strip()
        self._git(self.repo_root, "worktree", "add", "--detach", str(self.path), self.base_sha)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.path is not None:
            subprocess.run(["git", "worktree", "remove", "--force", str(self.path)], cwd=self.repo_root, capture_output=True, check=False)  # nosec B603 B607
            shutil.rmtree(self.path.parent, ignore_errors=True)

    def apply(self, diff: str) -> CommandResult:
        assert self.path is not None
        patch = self.path.parent / "proposal.diff"
        patch.write_text(diff, encoding="utf-8")
        check = self.run(["git", "apply", "--check", "--whitespace=nowarn", str(patch)])
        if check.exit_code != 0:
            return check
        return self.run(["git", "apply", "--whitespace=nowarn", str(patch)])

    def changed_files(self) -> list[str]:
        assert self.path is not None
        out = self._git(self.path, "status", "--porcelain", "--untracked-files=all")
        return sorted(line[3:] for line in out.splitlines() if line.strip())

    def read(self, rel: str) -> bytes:
        assert self.path is not None
        return (self.path / rel).read_bytes()

    def run(self, argv: list[str], *, timeout: float = 900.0) -> CommandResult:
        assert self.path is not None
        env = {k: os.environ[k] for k in SAFE_ENV_KEYS if k in os.environ}
        env["PYTHONPATH"] = str(self.path)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        started = time.monotonic()
        try:
            proc = subprocess.run(argv, cwd=self.path, env=env, capture_output=True, text=True, timeout=timeout, check=False)  # nosec B603
            code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            code, output = 124, f"timed out after {timeout}s\n{exc.stdout or ''}"
        return CommandResult(list(argv), int(code), round(time.monotonic() - started, 3), str(output)[-4000:])

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)  # nosec B603 B607
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:500]}")
        return proc.stdout
