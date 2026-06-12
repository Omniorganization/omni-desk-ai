from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


TOKEN_RE = re.compile(
    r"(gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|Bearer\s+[A-Za-z0-9._~+/=-]+)",
    re.IGNORECASE,
)


@dataclass
class GitHubRepository:
    host: str
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class GitHubPreflightResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    repo_root: Optional[str] = None
    remote_name: str = "origin"
    remote_url: Optional[str] = None
    repository: Optional[str] = None
    authenticated: bool = False
    can_write: Optional[bool] = None
    head_published: Optional[bool] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "repo_root": self.repo_root,
            "remote_name": self.remote_name,
            "remote_url": self.remote_url,
            "repository": self.repository,
            "authenticated": self.authenticated,
            "can_write": self.can_write,
            "head_published": self.head_published,
        }


def sanitize_command_output(text: str) -> str:
    return TOKEN_RE.sub("[REDACTED]", text or "")


def parse_github_remote(url: str, default_host: str = "github.com") -> Optional[GitHubRepository]:
    cleaned = url.strip()
    patterns = [
        r"^https://(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
        r"^git@(?P<host>[^:]+):(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$",
        r"^ssh://git@(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
    ]
    for pattern in patterns:
        match = re.match(pattern, cleaned)
        if match:
            host = match.group("host")
            if host != default_host:
                return GitHubRepository(host=host, owner=match.group("owner"), name=match.group("repo"))
            return GitHubRepository(host=default_host, owner=match.group("owner"), name=match.group("repo"))
    return None


class GitHubPreflight:
    def __init__(
        self,
        repo_root: Path,
        *,
        remote_name: str = "origin",
        host: str = "github.com",
        runner: Optional[CommandRunner] = None,
    ):
        self.repo_root = repo_root.resolve()
        self.remote_name = remote_name
        self.host = host
        self.runner = runner

    def run(self, *, head: Optional[str] = None, require_write: bool = True) -> GitHubPreflightResult:
        result = GitHubPreflightResult(ok=False, repo_root=str(self.repo_root), remote_name=self.remote_name)

        if shutil.which("git") is None:
            result.errors.append("git is not installed or not on PATH")
            return result
        if shutil.which("gh") is None:
            result.errors.append("GitHub CLI `gh` is not installed or not on PATH")
            return result

        root = self._git(["rev-parse", "--show-toplevel"])
        if root.returncode != 0:
            result.errors.append("current repo_root is not a git repository")
            result.warnings.append(sanitize_command_output(root.stderr or root.stdout).strip())
            return result
        result.repo_root = root.stdout.strip() or str(self.repo_root)

        remote = self._git(["remote", "get-url", self.remote_name])
        if remote.returncode != 0:
            result.errors.append(f"git remote `{self.remote_name}` is not configured")
            result.warnings.append(sanitize_command_output(remote.stderr or remote.stdout).strip())
            return result
        result.remote_url = remote.stdout.strip()

        repo = parse_github_remote(result.remote_url, self.host)
        if repo is None:
            result.errors.append(f"git remote `{self.remote_name}` is not a supported GitHub URL")
            return result
        result.repository = repo.full_name

        auth = self._gh(["auth", "status", "-h", repo.host])
        if auth.returncode != 0:
            result.errors.append(f"GitHub CLI is not authenticated for {repo.host}")
            result.warnings.append(sanitize_command_output(auth.stderr or auth.stdout).strip())
            return result
        result.authenticated = True

        if require_write:
            permission = self._gh(["api", f"repos/{repo.full_name}", "--jq", ".permissions.push"])
            if permission.returncode != 0:
                result.errors.append(f"unable to verify write permission for {repo.full_name}")
                result.warnings.append(sanitize_command_output(permission.stderr or permission.stdout).strip())
                return result
            result.can_write = permission.stdout.strip().lower() == "true"
            if result.can_write is False:
                result.errors.append(f"GitHub token can read {repo.full_name} but does not have push/write permission")
                return result

        if head:
            published = self._git(["ls-remote", "--heads", self.remote_name, head])
            if published.returncode != 0:
                result.errors.append(f"unable to check whether branch `{head}` exists on `{self.remote_name}`")
                result.warnings.append(sanitize_command_output(published.stderr or published.stdout).strip())
                return result
            result.head_published = bool(published.stdout.strip())
            if not result.head_published:
                result.errors.append(f"head branch `{head}` has not been pushed to `{self.remote_name}`")
                return result

        result.ok = not result.errors
        return result

    def _git(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return self._run(["git", *args])

    def _gh(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return self._run(["gh", *args])

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if self.runner is not None:
            return self.runner(argv)
        return subprocess.run(
            argv,
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
