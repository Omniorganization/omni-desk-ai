#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


CONTRACT: dict[str, tuple[str, ...]] = {
    "omnidesk_agent/appsync/postgres_migrations.py": (
        "omnidesk_appsync_schema_migrations",
        "atomic_chat_requests_and_append_only_events",
        "pg_advisory_xact_lock",
        "assert_appsync_schema_current",
    ),
    "omnidesk_agent/appsync/migrated_postgres_store.py": (
        "PsycopgConnectionPool",
        "pg_advisory_lock",
        "assert_appsync_schema_current",
        "direct_transactional_repository",
    ),
    "omnidesk_agent/appsync/lease_safe_chat_repository.py": (
        "ChatLeaseLost",
        "lease_owner=%s",
        "omnidesk_appsync_chat_stream_events",
    ),
    "omnidesk_agent/appsync/strict_json_store.py": (
        "CorruptAppSyncState",
        "os.replace",
        "fail_closed_and_quarantine",
    ),
    "omnidesk_agent/appsync/factory.py": (
        "JSON AppSync storage is development-only",
        "OMNIDESK_APPSYNC_POSTGRES_POOL_SIZE",
    ),
    ".github/workflows/ci.yml": (
        "governance:",
        "postgres-integration:",
        "matrix.python-version == '3.11'",
        "cancel-in-progress: true",
    ),
    ".github/workflows/security.yml": (
        "javascript-typescript",
        "npm audit --audit-level=high",
        "cargo-audit",
        "osv-scanner",
    ),
    ".github/workflows/tri-app-quality.yml": (
        "desktop-tauri-macos:",
        "desktop-tauri-windows:",
        "cargo clippy",
        "flutter build ios --simulator --debug",
    ),
    "deploy/kubernetes/helm/omnidesk/templates/migrate-job.yaml": (
        "pre-install,pre-upgrade",
        "omnidesk_agent.appsync.migrate",
        "namespace: {{ .Release.Namespace }}",
    ),
    ".github/dependabot.yml": (
        "package-ecosystem: pip",
        "package-ecosystem: npm",
        "package-ecosystem: cargo",
        "package-ecosystem: pub",
        "package-ecosystem: github-actions",
    ),
}


def check(root: Path) -> list[str]:
    issues: list[str] = []
    for rel, snippets in CONTRACT.items():
        path = root / rel
        if not path.exists():
            issues.append(f"missing industrial remediation asset: {rel}")
            continue
        text = path.read_text(encoding="utf-8")
        for snippet in snippets:
            if snippet not in text:
                issues.append(f"{rel} missing remediation contract: {snippet}")

    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    for requirement in (
        "fastapi>=0.129,<1",
        "pydantic>=2.7,<3",
        "psycopg[binary]>=3.2,<4",
    ):
        if requirement not in pyproject:
            issues.append(f"pyproject.toml missing bounded dependency: {requirement}")

    canonical = (root / "VERSION").read_text(encoding="utf-8").strip()
    if f'version = "{canonical}"' not in pyproject:
        issues.append("pyproject.toml does not match canonical VERSION")
    package_init = (root / "omnidesk_agent/__init__.py").read_text(encoding="utf-8")
    if f'__version__ = "{canonical}"' not in package_init:
        issues.append("package __version__ does not match canonical VERSION")
    readme = (root / "README.md").read_text(encoding="utf-8")
    if f"当前版本是 `{canonical}`。" not in readme:
        issues.append("README release boundary does not match canonical VERSION")

    templates = root / "deploy/kubernetes/helm/omnidesk/templates"
    for path in templates.glob("*.yaml"):
        if "namespace: omnidesk" in path.read_text(encoding="utf-8"):
            issues.append(f"hard-coded Helm namespace remains: {path.relative_to(root)}")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify source-remediable P0/P1/P2 industrial closure.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    issues = check(Path(args.root))
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("industrial P0/P1/P2 remediation contract verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
