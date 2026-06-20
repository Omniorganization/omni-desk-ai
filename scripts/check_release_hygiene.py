#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

FORBIDDEN_NAMES = {
    ".pytest_cache",
    ".ruff_cache",
    ".npm-cache",
    "__pycache__",
    ".mypy_cache",
    ".serena",
    ".venv",
    ".next",
    ".dart_tool",
    "dist",
    "build",
    "target",
    "node_modules",
    "htmlcov",
    "coverage",
    "__MACOSX",
    ".git",
}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".tsbuildinfo"}
FORBIDDEN_FILES = {
    ".coverage",
    "coverage.json",
    "coverage.xml",
    ".DS_Store",
    ".env",
    "npm-debug.log",
    "yarn-error.log",
}
FORBIDDEN_RUNTIME_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".pem", ".key"}
FORBIDDEN_RUNTIME_FILES = {"audit.log", "gmail_token.json", "oauth_token.json", "access_token.json", "refresh_token.json"}


def _is_source_root(base: Path) -> bool:
    return (base / "pyproject.toml").is_file() and (base / "omnidesk_agent").is_dir()


def _is_generated_package_artifact(path: Path) -> bool:
    name = path.name
    if path.is_dir() and (name.startswith("Omni-desk-AI-") or name.startswith("OmniDesk-AI-")):
        return True
    return path.is_file() and name.startswith("Omni-desk-AI-") and name.endswith(".zip")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check that a release tree contains no generated, cache, VCS, or platform metadata artifacts.")
    parser.add_argument("root", nargs="?", default=".", help="Tree to inspect")
    parser.add_argument(
        "--allow-vcs",
        action="store_true",
        help="Allow a top-level .git directory when checking a live source checkout. Do not use for release packages.",
    )
    return parser.parse_args(argv)


def main(*argv: str) -> int:
    args = _parse_args(list(argv))
    base = Path(args.root).resolve()
    source_root = _is_source_root(base)
    violations: list[str] = []
    for path in base.rglob("*"):
        rel_path = path.relative_to(base)
        rel = str(rel_path)
        parts = rel_path.parts
        if args.allow_vcs and parts and parts[0] == ".git":
            continue
        if source_root and len(parts) == 1 and _is_generated_package_artifact(path):
            violations.append(rel)
            continue
        if any(part in FORBIDDEN_NAMES for part in parts):
            violations.append(rel)
            continue
        if path.is_file() and (path.suffix in FORBIDDEN_SUFFIXES or path.name in FORBIDDEN_FILES):
            violations.append(rel)
        elif path.is_file() and (path.suffix.lower() in FORBIDDEN_RUNTIME_SUFFIXES or path.name.lower() in FORBIDDEN_RUNTIME_FILES):
            violations.append(rel)
    if violations:
        print("Release package contains generated/cache/VCS artifacts:", file=sys.stderr)
        for item in violations[:200]:
            print(f"  {item}", file=sys.stderr)
        if len(violations) > 200:
            print(f"  ... {len(violations) - 200} more", file=sys.stderr)
        return 1
    print("release hygiene ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
