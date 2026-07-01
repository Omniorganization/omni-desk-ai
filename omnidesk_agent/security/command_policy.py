from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

# Shared command allowlist used by ShellTool and the remote sandbox runner.
# Keep this narrow: all entries are argv prefixes, never shell strings.
SAFE_CI_ALLOWED_PREFIXES: list[list[str]] = [
    ["python", "-m", "compileall"],
    ["python3", "-m", "compileall"],
    ["pytest"],
    ["ruff", "check"],
    ["git", "status"],
    ["git", "diff"],
    ["git", "branch"],
    ["git", "log"],
    ["git", "ls-tree"],
]

UPGRADE_ALLOWED_PREFIXES: list[list[str]] = [
    ["git", "add"],
    ["git", "commit"],
    ["git", "pull"],
    ["git", "push"],
    ["pip", "install", "-e"],
    ["python3", "-m", "pip", "install", "-e"],
]

READONLY_PREFIXES: list[list[str]] = [
    ["python", "-m", "compileall"],
    ["python3", "-m", "compileall"],
    ["pytest"],
    ["ruff", "check"],
    ["git", "status"],
    ["git", "diff"],
    ["git", "branch"],
    ["git", "log"],
    ["git", "ls-tree"],
]


def argv_matches_prefix(argv: Sequence[str], prefix: Sequence[str]) -> bool:
    return len(argv) >= len(prefix) and list(argv[: len(prefix)]) == list(prefix)


def _safe_git_add_args(argv: Sequence[str], workspace_root: Path | None = None) -> bool:
    if len(argv) <= 2:
        return False
    root = workspace_root.expanduser().resolve() if workspace_root else None
    for raw in argv[2:]:
        value = str(raw)
        path = Path(value)
        if value in {"", ".", "--all", "-A"} or value.startswith("-"):
            return False
        if path.is_absolute() or ".." in path.parts:
            return False
        if root is not None:
            candidate = (root / path).resolve(strict=False)
            if candidate != root and root not in candidate.parents:
                return False
            if candidate.exists() and candidate.is_symlink():
                return False
    return True


def _safe_git_push_args(argv: Sequence[str]) -> bool:
    if len(argv) != 4:
        return False
    remote = str(argv[2])
    refspec = str(argv[3])
    return remote == "origin" and (
        refspec.startswith("HEAD:codex/")
        or refspec.startswith("HEAD:repair/")
        or refspec.startswith("HEAD:self-upgrade/")
    )


def _safe_editable_install_args(argv: Sequence[str], workspace_root: Path | None = None) -> bool:
    target = str(argv[-1]) if argv else ""
    if target != ".":
        return False
    return workspace_root is not None and workspace_root.expanduser().resolve().exists()


def _prefix_args_safe(argv: Sequence[str], prefix: Sequence[str], workspace_root: Path | None = None) -> bool:
    prefix_list = list(prefix)
    if prefix_list == ["git", "add"]:
        return _safe_git_add_args(argv, workspace_root)
    if prefix_list == ["git", "push"]:
        return _safe_git_push_args(argv)
    if prefix_list in (["pip", "install", "-e"], ["python3", "-m", "pip", "install", "-e"]):
        return _safe_editable_install_args(argv, workspace_root)
    return True


def argv_allowed(
    argv: Sequence[str],
    prefixes: Iterable[Sequence[str]] = SAFE_CI_ALLOWED_PREFIXES,
    *,
    workspace_root: Path | None = None,
) -> bool:
    return any(
        argv_matches_prefix(argv, prefix) and _prefix_args_safe(argv, prefix, workspace_root)
        for prefix in prefixes
    )


def readonly_command(argv: Sequence[str]) -> bool:
    return argv_allowed(argv, READONLY_PREFIXES)
