from __future__ import annotations

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


def argv_allowed(argv: Sequence[str], prefixes: Iterable[Sequence[str]] = SAFE_CI_ALLOWED_PREFIXES) -> bool:
    return any(argv_matches_prefix(argv, prefix) for prefix in prefixes)


def readonly_command(argv: Sequence[str]) -> bool:
    return argv_allowed(argv, READONLY_PREFIXES)
