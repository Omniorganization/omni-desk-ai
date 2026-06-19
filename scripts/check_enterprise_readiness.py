#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REQUIRED_FILES = [
    "omnidesk_agent/repositories/base.py",
    "omnidesk_agent/repositories/sqlite.py",
    "omnidesk_agent/repositories/postgres.py",
    "omnidesk_agent/repositories/runtime.py",
    "omnidesk_agent/repositories/health.py",
    "omnidesk_agent/observability_probe.py",
    "omnidesk_agent/security/break_glass.py",
    "scripts/production_closure_drill.py",
    "scripts/check_kubernetes_contract.py",
    ".github/workflows/production-closure-drill.yml",
    "omnidesk_agent/observability_otel.py",
    "omnidesk_agent/security/dual_approval.py",
    "omnidesk_agent/security/audit_worm.py",
    "deploy/observability/otel-collector.yaml",
    "omnidesk_agent/self_learning/promotion/policy.py",
    "scripts/list_cosign_artifacts.py",
    "scripts/read_release_metadata.py",
    "scripts/check_audit_checkpoint.py",
    "deploy/kubernetes/networkpolicy.yaml",
    "deploy/kubernetes/podsecurity.yaml",
    "deploy/kubernetes/external-secret.yaml",
    "deploy/kubernetes/service-monitor.yaml",
    "deploy/kubernetes/helm/omnidesk/Chart.yaml",
    "docs/ENTERPRISE_GA13_RUNTIME.md",
    "docs/ENTERPRISE_GA14_PRODUCTION_CLOSURE.md",
]

REQUIRED_RELEASE_SNIPPETS = [
    "docker buildx imagetools inspect",
    'echo "OMNIDESK_IMAGE_DIGEST=$digest"',
    "python scripts/write_slsa_provenance.py",
    "python scripts/list_cosign_artifacts.py dist --null",
    'cosign sign --yes "$OMNIDESK_IMAGE_REF@$OMNIDESK_IMAGE_DIGEST"',
]

FORBIDDEN_RELEASE_SNIPPETS = [
    "inputs.image_digest",
    "--build-arg OMNIDESK_IMAGE_DIGEST",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check GA14 enterprise production-closure assets and closed runtime/release loops.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root)
    issues: list[str] = []
    for rel in REQUIRED_FILES:
        if not (root / rel).exists():
            issues.append(f"missing enterprise hardening asset: {rel}")

    release = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
    for snippet in REQUIRED_RELEASE_SNIPPETS:
        if snippet not in release:
            issues.append(f"release.yml missing closed-loop snippet: {snippet}")
    for snippet in FORBIDDEN_RELEASE_SNIPPETS:
        if snippet in release:
            issues.append(f"release.yml must not contain manual/circular digest snippet: {snippet}")

    postgres = (root / "omnidesk_agent/repositories/postgres.py").read_text(encoding="utf-8")
    runtime_repo = (root / "omnidesk_agent/repositories/runtime.py").read_text(encoding="utf-8")
    daemon = (root / "omnidesk_agent/daemon.py").read_text(encoding="utf-8")
    for snippet in ["FOR UPDATE SKIP LOCKED", "transactional_outbox", "multi_instance_safe=True"]:
        if snippet not in postgres:
            issues.append(f"postgres repository missing: {snippet}")
    for snippet in ["storage.require_multi_instance_safe", "build_repository_factory", "storage_plan"]:
        if snippet not in runtime_repo + daemon:
            issues.append(f"runtime storage closure missing: {snippet}")
    runtime_state = (root / "omnidesk_agent/repositories/postgres_state.py").read_text(encoding="utf-8") if (root / "omnidesk_agent/repositories/postgres_state.py").exists() else ""
    sqlite_factory = (root / "omnidesk_agent/repositories/sqlite.py").read_text(encoding="utf-8") if (root / "omnidesk_agent/repositories/sqlite.py").exists() else ""
    for snippet in ["WormAuditCheckpoint"]:
        if snippet not in daemon:
            issues.append(f"runtime security closure missing: {snippet}")
    for snippet in ["dual_approval_store", "break_glass_store"]:
        if snippet not in daemon or snippet not in runtime_state + sqlite_factory:
            issues.append(f"runtime security factory closure missing: {snippet}")
    if 'cfg.workspace.root / "approvals.sqlite3"' in daemon or 'cfg.workspace.root / "jobs.sqlite3"' in daemon or 'cfg.workspace.root / "runs.sqlite3"' in daemon:
        issues.append("daemon must not directly construct core SQLite state stores in the production runtime path")

    server = (root / "omnidesk_agent/server.py").read_text(encoding="utf-8")
    for snippet in ["OTLPHttpExporter", "parse_traceparent", '"http.request"']:
        if snippet not in server:
            issues.append(f"server OTLP/runtime trace wiring missing: {snippet}")

    promote = (root / ".github/workflows/promote-production.yml").read_text(encoding="utf-8")
    for snippet in [
        'cosign verify "$OMNIDESK_IMAGE_REF@$OMNIDESK_IMAGE_DIGEST"',
        'cosign verify-attestation "$OMNIDESK_IMAGE_REF@$OMNIDESK_IMAGE_DIGEST"',
    ]:
        if snippet not in promote:
            issues.append(f"promotion image signature gate missing: {snippet}")

    policy = (root / "omnidesk_agent/self_learning/promotion/policy.py").read_text(encoding="utf-8")
    for snippet in ["min_sample_size_per_arm", "min_confidence", "safety violation hard block", "requires_human_approval"]:
        if snippet not in policy:
            issues.append(f"learning promotion policy missing: {snippet}")
    closure = (root / "scripts/production_closure_drill.py").read_text(encoding="utf-8")
    for snippet in ["probe_otlp_endpoint", "check_repository_factory", "check_kubernetes_contract_main", "WormAuditCheckpoint"]:
        if snippet not in closure:
            issues.append(f"production closure drill missing: {snippet}")

    kube_check = (root / "scripts/check_kubernetes_contract.py").read_text(encoding="utf-8")
    for snippet in ["runAsNonRoot: true", "readOnlyRootFilesystem: true", "sha256"]:
        if snippet not in kube_check:
            issues.append(f"Kubernetes contract checker missing: {snippet}")

    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("enterprise readiness contract verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
