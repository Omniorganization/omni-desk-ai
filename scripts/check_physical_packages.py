#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PACKAGES = {
    "agent-runtime": "omnidesk_agent_runtime",
    "connector-sdk": "omnidesk_connector_sdk",
    "policy-engine": "omnidesk_policy_engine",
    "approval-core": "omnidesk_approval_core",
    "audit-core": "omnidesk_audit_core",
    "memory-core": "omnidesk_memory_core",
}

REQUIRED_PYPROJECT_TOKENS = [
    "[build-system]",
    "setuptools.build_meta",
    "[project]",
    "requires-python",
]


def _check_package(root: Path, slug: str, module: str) -> list[str]:
    issues: list[str] = []
    package_root = root / "packages" / slug
    required = [
        package_root / "README.md",
        package_root / "pyproject.toml",
        package_root / "src" / module / "__init__.py",
        package_root / "src" / module / "boundary.py",
    ]
    for path in required:
        if not path.exists():
            issues.append(f"packages/{slug}: missing {path.relative_to(root)}")
    pyproject = package_root / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8")
        for token in REQUIRED_PYPROJECT_TOKENS:
            if token not in text:
                issues.append(f"packages/{slug}: pyproject missing {token}")
        if module not in text:
            issues.append(f"packages/{slug}: pyproject does not expose module {module}")
    boundary = package_root / "src" / module / "boundary.py"
    if boundary.exists():
        text = boundary.read_text(encoding="utf-8")
        for token in ["BOUNDARY_NAME", "OWNED_SOURCE_PATHS", "FORBIDDEN_IMPORT_PREFIXES"]:
            if token not in text:
                issues.append(f"packages/{slug}: boundary.py missing {token}")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check physical package boundary skeletons.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    issues: list[str] = []
    for slug, module in PACKAGES.items():
        issues.extend(_check_package(root, slug, module))
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print(f"physical package boundaries ok: {len(PACKAGES)} packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
