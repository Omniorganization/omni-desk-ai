#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable


def _read(root: Path, rel: str) -> str:
    path = root / rel
    if not path.exists():
        raise RuntimeError(f"missing required file: {rel}")
    return path.read_text(encoding="utf-8")


def _json(root: Path, rel: str) -> dict:
    return json.loads(_read(root, rel))


def _contains(root: Path, rel: str, *needles: str) -> bool:
    text = _read(root, rel)
    return all(needle in text for needle in needles)


def _contains_any(root: Path, rel: str, needles: tuple[str, ...]) -> bool:
    text = _read(root, rel)
    return any(needle in text for needle in needles)


def _file_exists(root: Path, rel: str) -> bool:
    return (root / rel).exists()


def _source_engineering(root: Path) -> list[tuple[str, bool]]:
    return [
        ("version consistency checker exists", _file_exists(root, "scripts/check_version_consistency.py")),
        ("release hygiene checker exists", _file_exists(root, "scripts/check_release_hygiene.py")),
        ("enterprise readiness checker exists", _file_exists(root, "scripts/check_enterprise_readiness.py")),
        ("kubernetes contract checker exists", _file_exists(root, "scripts/check_kubernetes_contract.py")),
        ("production evidence manifest is explicit", _contains(root, "release/production-evidence.manifest.json", "source_gate_ready_external_evidence_blocked", "external_evidence_required")),
    ]


def _release_governance(root: Path) -> list[tuple[str, bool]]:
    branch_policy = _json(root, ".github/branch-protection.required.json")
    required_checks = set(branch_policy.get("required_status_checks", []))
    required_jobs = set(branch_policy.get("required_jobs", []))
    return [
        ("release policy workflow runs contract checks", _contains(root, ".github/workflows/release-policy.yml", "check_branch_protection_contract.py .", "check_main_verification_contract.py .")),
        ("release policy runs attack surface gate", _contains(root, ".github/workflows/release-policy.yml", "check_security_attack_surface.py .")),
        ("main verification emits digest-bound evidence", _contains(root, ".github/workflows/main-verification.yml", "main-verification-evidence-${{ github.sha }}", "hashlib.sha256")),
        ("main verification includes attack surface gate", _contains(root, ".github/workflows/main-verification.yml", "check_security_attack_surface.py .", "security_attack_surface")),
        ("branch protection contract requires release governance checks", {"Release Policy", "CI", "Security", "Security Attack Surface Gate", "Tri-App Quality Gate", "Source Maturity Closure"}.issubset(required_checks)),
        ("branch protection contract requires release governance jobs", {"release-policy", "external-ga-evidence-contract", "security-attack-surface", "source-maturity-closure"}.issubset(required_jobs)),
        ("branch protection contract blocks pending checks", (branch_policy.get("merge_policy") or {}).get("block_pending_required_checks") is True),
    ]


def _security_supply_chain(root: Path) -> list[tuple[str, bool]]:
    return [
        ("security workflow policy checker exists", _file_exists(root, "scripts/check_security_workflow_policy.py")),
        ("security attack surface checker exists", _file_exists(root, "scripts/check_security_attack_surface.py")),
        ("security attack surface workflow exists", _file_exists(root, ".github/workflows/security-attack-surface.yml")),
        ("security workflow runs attack surface checker", _contains(root, ".github/workflows/security.yml", "check_security_attack_surface.py .")),
        ("supply chain standard checker exists", _file_exists(root, "scripts/check_supply_chain_standard.py")),
        ("production install policy checker exists", _file_exists(root, "scripts/check_production_install_policy.py")),
        (
            "release workflow uses external evidence gate",
            _contains_any(root, ".github/workflows/release.yml", ("check_external_ga_evidence.py", "check_real_ga_complete.py"))
            and _contains(root, ".github/workflows/release.yml", "write_real_ga_evidence_summary.py"),
        ),
        ("AGENTS forbid fabricated evidence", _contains(root, "AGENTS.md", "Do not fabricate release evidence")),
    ]


def _tri_app_engineering(root: Path) -> list[tuple[str, bool]]:
    workflow = ".github/workflows/tri-app-quality.yml"
    return [
        ("web admin quality job exists", _contains(root, workflow, "web-admin:", "npm run typecheck", "npm run build")),
        ("desktop tauri quality job exists", _contains(root, workflow, "desktop-tauri:", "cargo check --locked")),
        ("android appbundle quality job exists", _contains(root, workflow, "mobile-flutter:", "flutter build appbundle --release")),
        ("iOS source quality job exists", _contains(root, workflow, "mobile-ios-simulator:", "--mode mobile-ios-source")),
        ("iOS signed release gate remains declared", _contains(root, workflow, "mobile-ios-release:", "flutter build ipa --release")),
    ]


def _offline_sync_reconnect(root: Path) -> list[tuple[str, bool]]:
    return [
        ("offline sync applying store exists", _contains(root, "omnidesk_agent/appsync/offline_sync_apply.py", "ApplyingAppSyncStore", "apply_uploaded_operations")),
        ("postgres applying store exists", _contains(root, "omnidesk_agent/appsync/postgres_applying_sync.py", "ApplyingDurablePostgresAppSyncStore")),
        ("factory routes to applying stores", _contains(root, "omnidesk_agent/appsync/factory.py", "ApplyingAppSyncStore", "ApplyingDurablePostgresAppSyncStore")),
        ("reconnect worker test exists", _file_exists(root, "tests/test_reconnect_sync_worker.py")),
        ("offline sync updater hardening test exists", _file_exists(root, "tests/test_offline_sync_updater_hardening.py")),
    ]


CATEGORY_CHECKS: dict[str, Callable[[Path], list[tuple[str, bool]]]] = {
    "source_engineering": _source_engineering,
    "release_governance": _release_governance,
    "security_supply_chain": _security_supply_chain,
    "tri_app_engineering": _tri_app_engineering,
    "offline_sync_reconnect": _offline_sync_reconnect,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate source-only maturity closure contracts for OmniDesk.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--write-report", default="")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    results: dict[str, dict[str, object]] = {}
    failures: list[str] = []

    for category, checker in CATEGORY_CHECKS.items():
        checks = checker(root)
        check_results = [{"check": name, "passed": passed} for name, passed in checks]
        for name, passed in checks:
            if not passed:
                failures.append(f"{category}: {name}")
        results[category] = {
            "score": 100 if all(passed for _, passed in checks) else 0,
            "checks": check_results,
        }

    report = {
        "schema": "omnidesk-source-maturity-closure/v1",
        "status": "passed" if not failures else "failed",
        "scope": "source_only_not_customer_distribution_ga",
        "scores": {category: value["score"] for category, value in results.items()},
        "results": results,
        "distribution_ga_boundary": "External signer, store, push, rollback, backup/restore, soak, and failure-injection evidence remains a separate real-world gate.",
    }
    if args.write_report:
        Path(args.write_report).write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "scores": report["scores"]}, ensure_ascii=False, sort_keys=True))
    if failures:
        for failure in failures:
            print(f"BLOCKER {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
