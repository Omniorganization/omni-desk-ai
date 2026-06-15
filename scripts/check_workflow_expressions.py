#!/usr/bin/env python3
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path

BROKEN_PATTERNS = [
    re.compile(r"production-rc\d+-tri-app-hardening\s+inputs\.expected_version\s*}}"),
    re.compile(r"enterprise-staging-tri-app-hardening\s+inputs\.expected_version\s*}}"),
    re.compile(r"controlled-staging-tri-app-hardening\s+inputs\.expected_version\s*}}"),
]
REQUIRED_VALUES = {
    "EXPECTED_VERSION": "${{ inputs.expected_version }}",
    "OMNIDESK_EXPECTED_VERSION": "${{ inputs.expected_version }}",
}
REQUIRED_FILES = [
    ".github/workflows/deploy-staging.yml",
    ".github/workflows/promote-production.yml",
]

def _yaml_value(line: str) -> tuple[str, str] | None:
    match = re.match(r"^\s*([A-Z_]+):\s*(.+?)\s*$", line)
    if not match:
        return None
    return match.group(1), match.group(2)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check GitHub Actions expression hygiene.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    failures: list[str] = []
    for path in sorted((root / ".github" / "workflows").glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(root).as_posix()
        for pattern in BROKEN_PATTERNS:
            if pattern.search(text):
                failures.append(f"{rel}: broken expression matched {pattern.pattern}")
    for rel in REQUIRED_FILES:
        path = root / rel
        seen: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            parsed = _yaml_value(line)
            if parsed and parsed[0] in REQUIRED_VALUES:
                seen[parsed[0]] = parsed[1]
        for key, expected in REQUIRED_VALUES.items():
            if seen.get(key) != expected:
                failures.append(f"{rel}: {key} must be {expected!r}, got {seen.get(key)!r}")
    if failures:
        print("workflow expression check failed:", file=sys.stderr)
        for item in failures:
            print(f"  {item}", file=sys.stderr)
        return 1
    print("workflow expression hygiene ok")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
