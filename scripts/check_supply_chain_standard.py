#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REQUIRED_RELEASE_SNIPPETS = [
    "cosign sign-blob",
    "cosign attest-blob",
    "cosign sign --yes",
    "cosign attest --yes",
    "slsa-provenance",
    "gh attestation sign",
    "docker build",
    "docker push",
    "OMNIDESK_IMAGE_DIGEST",
    "docker buildx imagetools inspect",
]

REQUIRED_SUPPLY_CHAIN_SNIPPETS = [
    "cosign verify-blob",
    "cosign verify-attestation",
    "slsa-provenance",
    "in-toto",
    "gh attestation verify",
]

REQUIRED_PROMOTE_SNIPPETS = [
    "cosign verify-blob",
    'cosign verify "$OMNIDESK_IMAGE_REF@$OMNIDESK_IMAGE_DIGEST"',
    "cosign verify-attestation",
    "gh attestation verify",
    "release_metadata.json",
]

REQUIRED_FILES = [
    ".github/workflows/supply-chain.yml",
    ".github/workflows/soak-test.yml",
    ".github/workflows/alert-drill.yml",
    ".github/workflows/maintenance-drill.yml",
    "scripts/soak_test.py",
    "docs/RUNNING_GA_CHECKLIST.md",
    "deploy/systemd/omnidesk-agent.production.service",
    "deploy/systemd/omnidesk-backup.timer",
    "deploy/systemd/omnidesk-maintenance.timer",
    "deploy/systemd/logrotate.omnidesk",
    "scripts/maintenance_sqlite.py",
    "scripts/check_disk_guard.py",
    "scripts/check_alert_rules_fire.py",
    "omnidesk_agent/observability_tracing.py",
    "omnidesk_agent/observability_otel.py",
    "deploy/observability/otel-collector.yaml",
    "omnidesk_agent/repositories/postgres.py",
    "omnidesk_agent/self_learning/promotion/policy.py",
    "requirements.enterprise.lock",
]


def _contains_all(path: Path, snippets: list[str]) -> list[str]:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return [snippet for snippet in snippets if snippet not in text]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify standard supply-chain and running-GA governance assets.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root)
    failures: list[str] = []

    for rel in REQUIRED_FILES:
        if not (root / rel).exists():
            failures.append(f"missing required GA asset: {rel}")

    release_text = (root / ".github/workflows/release.yml").read_text(encoding="utf-8") if (root / ".github/workflows/release.yml").exists() else ""
    release_missing = [snippet for snippet in REQUIRED_RELEASE_SNIPPETS if snippet not in release_text]
    if release_missing:
        failures.append("release.yml missing snippets: " + ", ".join(release_missing))
    if "cosign attest-blob" not in release_text and "in-toto" not in release_text:
        failures.append("release.yml must include in-toto-style blob attestations")

    supply_missing = _contains_all(root / ".github/workflows/supply-chain.yml", REQUIRED_SUPPLY_CHAIN_SNIPPETS)
    if supply_missing:
        failures.append("supply-chain.yml missing snippets: " + ", ".join(supply_missing))

    promote_missing = _contains_all(root / ".github/workflows/promote-production.yml", REQUIRED_PROMOTE_SNIPPETS)
    if promote_missing:
        failures.append("promote-production.yml missing snippets: " + ", ".join(promote_missing))

    dockerfile = root / "Dockerfile"
    if dockerfile.exists():
        dtext = dockerfile.read_text(encoding="utf-8")
        if "requirements.enterprise.lock" not in dtext:
            failures.append("Dockerfile must install enterprise dependencies from requirements.enterprise.lock")
        forbidden = ["psycopg[binary]>=", "pip install --no-cache-dir \"psycopg", "pip install psycopg"]
        for snippet in forbidden:
            if snippet in dtext:
                failures.append("Dockerfile contains non hash-locked dependency install: " + snippet)

    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print("supply-chain standard contract verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
