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
    "actions/attest-build-provenance@",
    "docker build",
    "docker push",
    "OMNIDESK_IMAGE_DIGEST",
    "docker buildx imagetools inspect",
    "scripts/finalize_release_payload.py dist",
    "environment: release",
]

# Verifiers trust only release.yml running from main, pin the attestation source
# ref, and verify Ed25519 signatures with the committed public key.
MAIN_ONLY_SIGNER_IDENTITY = "release\\.yml@refs/heads/main$'"
SOURCE_REF_SNIPPETS = [
    "RELEASE_SOURCE_REF: refs/heads/main",
    '--source-ref "$RELEASE_SOURCE_REF"',
]
ED25519_VERIFIER_SNIPPETS = [
    "verify_release_signatures.py",
    'OMNIDESK_REQUIRE_ED25519_RELEASE_SIGNATURES: "1"',
]
VERIFIER_FORBIDDEN_SNIPPETS = [
    "refs/tags/",
    "tags/v",
    "OMNIDESK_RELEASE_SIGNING_KEY",
    "OMNIDESK_RELEASE_SIGNING_ED25519_KEY",
]

REQUIRED_SUPPLY_CHAIN_SNIPPETS = [
    "cosign verify-blob",
    "cosign verify-attestation",
    "slsa-provenance",
    "in-toto",
    "gh attestation verify",
    MAIN_ONLY_SIGNER_IDENTITY,
    *SOURCE_REF_SNIPPETS,
]

REQUIRED_PROMOTE_SNIPPETS = [
    "cosign verify-blob",
    'cosign verify "$OMNIDESK_IMAGE_REF@$OMNIDESK_IMAGE_DIGEST"',
    "cosign verify-attestation",
    "gh attestation verify",
    "release_metadata.json",
    MAIN_ONLY_SIGNER_IDENTITY,
    *SOURCE_REF_SNIPPETS,
    *ED25519_VERIFIER_SNIPPETS,
]

REQUIRED_STAGING_SNIPPETS = [
    "cosign verify-blob",
    "gh attestation verify",
    MAIN_ONLY_SIGNER_IDENTITY,
    *SOURCE_REF_SNIPPETS,
    *ED25519_VERIFIER_SNIPPETS,
]

REQUIRED_ROLLBACK_SNIPPETS = [
    "gh attestation verify",
    *SOURCE_REF_SNIPPETS,
    *ED25519_VERIFIER_SNIPPETS,
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

    for name, snippets in (
        ("deploy-staging.yml", REQUIRED_STAGING_SNIPPETS),
        ("rollback-drill.yml", REQUIRED_ROLLBACK_SNIPPETS),
    ):
        missing = _contains_all(root / ".github/workflows" / name, snippets)
        if missing:
            failures.append(f"{name} missing snippets: " + ", ".join(missing))

    for name in ("supply-chain.yml", "promote-production.yml", "deploy-staging.yml", "rollback-drill.yml"):
        path = root / ".github/workflows" / name
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        forbidden = [snippet for snippet in VERIFIER_FORBIDDEN_SNIPPETS if snippet in text]
        if forbidden:
            failures.append(f"{name} must not trust release tags or hold release signing secrets: " + ", ".join(forbidden))
        verify_lines = [line for line in text.splitlines() if "gh attestation verify" in line and not line.strip().startswith("#")]
        if any("--source-ref" not in line for line in verify_lines):
            failures.append(f"{name}: every gh attestation verify must pin --source-ref")

    if (root / "release/release-signing-public-key.pem").exists():
        key_text = (root / "release/release-signing-public-key.pem").read_text(encoding="utf-8")
        if "PRIVATE KEY" in key_text:
            failures.append("release/release-signing-public-key.pem must contain only the public key")

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
