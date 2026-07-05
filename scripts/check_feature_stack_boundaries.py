#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

FOCUS_AREAS = {
    "self_learning": [
        "omnidesk_agent/self_learning/__init__.py",
        "omnidesk_agent/self_learning/policy.py",
        "omnidesk_agent/self_learning/approval.py",
        "omnidesk_agent/self_learning/validator.py",
        "omnidesk_agent/self_learning/rollback.py",
        "omnidesk_agent/self_learning/store.py",
        "omnidesk_agent/self_learning/promotion/engine.py",
        "tests/test_controlled_self_learning.py",
    ],
    "repair_contracts": [
        "omnidesk_agent/repair_contracts.py",
    ],
    "ga_evidence": [
        "release/external-ga-evidence.required.json",
        "scripts/check_external_ga_evidence.py",
        "scripts/check_real_ga_readiness_contract.py",
        "scripts/write_real_ga_evidence_summary.py",
        "Makefile",
    ],
    "multi_channel": [
        "omnidesk_agent/channels",
        "packages/connector-sdk/README.md",
        "apps/shared/omni-app-api.contract.json",
    ],
    "tri_app": [
        "apps/shared/omni-app-api.contract.json",
        "apps/web-admin-next/package.json",
        "apps/desktop-tauri/package.json",
        "apps/mobile-flutter/pubspec.yaml",
        "apps/web-admin-next/tests/api.test.ts",
        "apps/desktop-tauri/tests/api.test.ts",
        "apps/mobile-flutter/test/omni_api_test.dart",
        "scripts/check_typed_client_contracts.py",
        "tests/test_tri_app_foundation.py",
        ".github/workflows/tri-app-quality.yml",
    ],
}

FORBIDDEN_SELF_LEARNING_TEXT = {
    "merge_pull_request",
    "update_ref(",
    "enable_auto_merge",
    "git push",
    "git merge",
    "auto_merge=True",
    "production_updates_applied = True",
}

FORBIDDEN_LOCAL_REPAIR_DTO_TEXT = {
    "class SelfLearningEvidenceBundle",
    "class EvidenceBundle",
    "class PullRequestDraft",
}

REQUIRED_SELF_LEARNING_TEXT = {
    "omnidesk_agent/self_learning/policy.py": [
        "stage_1_cannot_apply_system_change",
        "stage_3_must_not_merge_pr",
    ],
    "omnidesk_agent/self_learning/promotion/engine.py": [
        "approval is not bound",
        "validation is not bound",
    ],
    "omnidesk_agent/self_learning/validator.py": [
        "code repair proposals require regression commands",
    ],
}

REQUIRED_REPAIR_CONTRACT_TEXT = {
    "omnidesk_agent/repair_contracts.py": [
        "class RepairEvidenceBundle",
        "class PullRequestDraft",
        "external_evidence_status",
        "artifact_hashes",
        "ready_for_review",
    ],
    "omnidesk_agent/self_learning/proposal_generator.py": [
        "from omnidesk_agent.repair_contracts import PullRequestDraft, RepairEvidenceBundle",
    ],
    "omnidesk_agent/self_upgrade/evidence_bundle.py": [
        "from omnidesk_agent.repair_contracts import RepairEvidenceBundle",
        "EvidenceBundle = RepairEvidenceBundle",
    ],
    "omnidesk_agent/self_upgrade/pr_generator.py": [
        "from omnidesk_agent.repair_contracts import PullRequestDraft",
    ],
}

REQUIRED_TYPED_CLIENT_CONTRACT_TEXT = {
    "scripts/check_typed_client_contracts.py": [
        "REQUIRED_SURFACE_ROUTES",
        "TYPED_TEST_FILES",
        "typed client contract tests verified",
    ],
    "apps/web-admin-next/tests/api.test.ts": [
        "WEB_ADMIN_TYPED_CLIENT_CONTRACT_CASES",
        "readonly TypedContractCase[]",
    ],
    "apps/desktop-tauri/tests/api.test.ts": [
        "DESKTOP_TYPED_CLIENT_CONTRACT_CASES",
        "readonly TypedContractCase[]",
    ],
    "apps/mobile-flutter/test/omni_api_test.dart": [
        "mobileTypedClientContractCases",
        "TypedClientContractCase",
    ],
    "Makefile": [
        "typed-client-contracts",
        "scripts/check_typed_client_contracts.py .",
    ],
    ".github/workflows/ci.yml": [
        "Typed client contract tests",
        "scripts/check_typed_client_contracts.py .",
    ],
    ".github/workflows/tri-app-quality.yml": [
        "Typed client contract coverage",
        "scripts/check_typed_client_contracts.py .",
    ],
}


def _exists(root: Path, rel: str) -> bool:
    return (root / rel).exists()


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def _check_required_paths(root: Path) -> list[str]:
    issues: list[str] = []
    for area, paths in FOCUS_AREAS.items():
        for rel in paths:
            if not _exists(root, rel):
                issues.append(f"{area}: missing required boundary path: {rel}")
    return issues


def _check_self_learning(root: Path) -> list[str]:
    issues: list[str] = []
    base = root / "omnidesk_agent" / "self_learning"
    for path in sorted(base.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(root)
        for token in FORBIDDEN_SELF_LEARNING_TEXT:
            if token in text:
                issues.append(f"self_learning: forbidden direct mutation token in {rel}: {token}")
    for rel, tokens in REQUIRED_SELF_LEARNING_TEXT.items():
        text = _read(root, rel)
        for token in tokens:
            if token not in text:
                issues.append(f"self_learning: missing guard token in {rel}: {token}")
    return issues


def _check_repair_contracts(root: Path) -> list[str]:
    issues: list[str] = []
    for rel, tokens in REQUIRED_REPAIR_CONTRACT_TEXT.items():
        text = _read(root, rel)
        for token in tokens:
            if token not in text:
                issues.append(f"repair_contracts: missing neutral contract token in {rel}: {token}")
    for rel in [
        "omnidesk_agent/self_learning/proposal_generator.py",
        "omnidesk_agent/self_upgrade/evidence_bundle.py",
        "omnidesk_agent/self_upgrade/pr_generator.py",
    ]:
        text = _read(root, rel)
        for token in FORBIDDEN_LOCAL_REPAIR_DTO_TEXT:
            if token in text:
                issues.append(f"repair_contracts: local DTO drift in {rel}: {token}")
    return issues


def _check_typed_client_contracts(root: Path) -> list[str]:
    issues: list[str] = []
    for rel, tokens in REQUIRED_TYPED_CLIENT_CONTRACT_TEXT.items():
        text = _read(root, rel)
        for token in tokens:
            if token not in text:
                issues.append(f"typed_client_contracts: missing token in {rel}: {token}")
    return issues


def _check_ga_evidence(root: Path) -> list[str]:
    issues: list[str] = []
    required = _read(root, "release/external-ga-evidence.required.json")
    checker = _read(root, "scripts/check_external_ga_evidence.py")
    categories = [
        "native_build",
        "signed_artifacts",
        "model_live_smoke",
        "bigseller_live_smoke",
        "push_delivery",
        "operations_drills",
    ]
    for category in categories:
        if category not in required:
            issues.append(f"ga_evidence: required contract misses {category}")
    for token in ["PLACEHOLDER_RE", "requires_artifact", "signature_verified"]:
        if token not in checker:
            issues.append(f"ga_evidence: checker misses fail-closed token {token}")
    return issues


def _check_tri_app(root: Path) -> list[str]:
    issues: list[str] = []
    readiness = _read(root, "scripts/check_tri_app_release_readiness.py")
    contract = _read(root, "apps/shared/omni-app-api.contract.json")
    for token in ["desktop", "mobile", "web_admin"]:
        if token not in contract:
            issues.append(f"tri_app: shared contract misses surface {token}")
    for token in ["flutter build appbundle --release", "cargo check --locked", "npm ci"]:
        if token not in readiness:
            issues.append(f"tri_app: readiness checker misses release gate {token}")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check stacked feature boundary controls.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    issues: list[str] = []
    issues.extend(_check_required_paths(root))
    issues.extend(_check_self_learning(root))
    issues.extend(_check_repair_contracts(root))
    issues.extend(_check_typed_client_contracts(root))
    issues.extend(_check_ga_evidence(root))
    issues.extend(_check_tri_app(root))
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("feature stack boundaries ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
