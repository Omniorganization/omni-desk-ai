#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REQUIRED_PATHS = [
    "README.md",
    "ARCHITECTURE.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "VERSION",
    "LICENSE",
    "pyproject.toml",
    "release-manifest.schema.json",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    ".github/workflows/security.yml",
    ".github/workflows/supply-chain.yml",
    "apps/README.md",
    "apps/web-admin-next/package.json",
    "apps/desktop-tauri/package.json",
    "apps/mobile-flutter/pubspec.yaml",
    "apps/shared/omni-app-api.contract.json",
    "packages/agent-runtime/README.md",
    "packages/connector-sdk/README.md",
    "packages/policy-engine/README.md",
    "packages/approval-core/README.md",
    "packages/audit-core/README.md",
    "packages/memory-core/README.md",
    "infra/docker/README.md",
    "infra/k8s/README.md",
    "infra/otel/README.md",
    "deploy/docker/docker-compose.full.yml",
    "deploy/kubernetes/helm/omnidesk/Chart.yaml",
    "deploy/observability/otel-collector.yaml",
    "tests/e2e/README.md",
    "tests/integration/README.md",
    "tests/load/README.md",
    "tests/unit/README.md",
    "release/external-ga-evidence.required.json",
]


def check(root: Path) -> list[str]:
    issues: list[str] = []
    for rel in REQUIRED_PATHS:
        if not (root / rel).exists():
            issues.append(f"missing required monorepo path: {rel}")
    tracked_package_dirs = [
        path.name
        for path in root.glob("Omni-desk-AI-*")
        if path.is_dir() and not path.name.endswith("-candidate") and not path.name.endswith("-candidate-full")
    ]
    # A preserved historical package directory is allowed only while root-level monorepo files exist.
    if tracked_package_dirs and not (root / "pyproject.toml").exists():
        issues.append("package-only root detected without root-level source files")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify OmniDesk root-level monorepo layout.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    issues = check(Path(args.root).resolve())
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("monorepo layout ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
