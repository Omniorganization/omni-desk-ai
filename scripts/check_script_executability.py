#!/usr/bin/env python3
from __future__ import annotations

import argparse
import stat
import sys
from pathlib import Path


def _workflow_direct_script_runs(root: Path) -> list[str]:
    issues: list[str] = []
    workflow_dir = root / ".github" / "workflows"
    if not workflow_dir.exists():
        return issues
    for workflow in sorted(workflow_dir.glob("*.yml")):
        for lineno, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("run: scripts/") and ".sh" in stripped:
                issues.append(f"{workflow.relative_to(root)}:{lineno}: use bash scripts/... for shell scripts")
    return issues


def _non_executable_shell_scripts(root: Path) -> list[str]:
    issues: list[str] = []
    scripts_dir = root / "scripts"
    for script in sorted(scripts_dir.glob("*.sh")):
        mode = script.stat().st_mode
        if not mode & stat.S_IXUSR:
            issues.append(f"{script.relative_to(root)} is not owner-executable")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check shell script invocation and executable-bit release contracts.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    issues = _workflow_direct_script_runs(root)
    issues.extend(_non_executable_shell_scripts(root))
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("script executability ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
