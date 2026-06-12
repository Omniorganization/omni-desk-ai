#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

FORBIDDEN_NAMES = {".pytest_cache", ".ruff_cache", "__pycache__", ".mypy_cache", ".serena", "dist", "build"}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo"}
FORBIDDEN_FILES = {".coverage", "coverage.json", "coverage.xml"}


def main(root: str = ".") -> int:
    base = Path(root).resolve()
    violations: list[str] = []
    for path in base.rglob("*"):
        rel = str(path.relative_to(base))
        if any(part in FORBIDDEN_NAMES for part in path.parts):
            violations.append(rel)
            continue
        if path.is_file() and (path.suffix in FORBIDDEN_SUFFIXES or path.name in FORBIDDEN_FILES):
            violations.append(rel)
    if violations:
        print("Release package contains generated/cache artifacts:", file=sys.stderr)
        for item in violations[:200]:
            print(f"  {item}", file=sys.stderr)
        if len(violations) > 200:
            print(f"  ... {len(violations) - 200} more", file=sys.stderr)
        return 1
    print("release hygiene ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*(sys.argv[1:] or [])))
