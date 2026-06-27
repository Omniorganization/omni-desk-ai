#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REQUIRED_FILES = (
    ".github/workflows/real-ga-readiness.yml",
    "scripts/check_live_branch_protection_contract.py",
    "scripts/check_main_verification_artifact_live.py",
    "scripts/check_model_live_smoke_evidence.py",
    "scripts/check_external_ga_evidence.py",
    "scripts/run_bigseller_live_smoke.py",
    "scripts/import_bigseller_live_smoke_evidence.py",
    "docs/REAL_GA_EVIDENCE_RUNBOOK_1.12.7.md",
)

WORKFLOW_SNIPPETS = (
    "name: Real GA Readiness",
    "check_live_branch_protection_contract.py",
    "check_main_verification_artifact_live.py",
    "check_external_ga_evidence.py",
    "check_model_live_smoke_evidence.py",
    "readiness_channel",
    "real-ga",
    "candidate",
)

MANIFEST_REQUIRED_KEYS = (
    "live_branch_protection_control_plane",
    "model_live_smoke",
    "bigseller_live_smoke",
    "native_build",
    "signed_artifacts",
    "push_delivery",
    "postgres_soak",
    "rollback_drill",
    "backup_restore_drill",
    "self_healing_failure_injection",
)

EXTERNAL_CHECK_SNIPPETS = (
    '"live_branch_protection"',
    '"model_live_smoke"',
    '"bigseller_live_smoke"',
    "github-branch-protection-live.json",
    "model-live-smoke.json",
    "integrations/bigseller-live-smoke.json",
)


def _read(root: Path, rel: str) -> str:
    path = root / rel
    if not path.exists():
        raise FileNotFoundError(rel)
    return path.read_text(encoding="utf-8")


def _check(condition: bool, failures: list[str], message: str) -> None:
    if not condition:
        failures.append(message)


def audit(root: Path) -> dict[str, object]:
    failures: list[str] = []

    for rel in REQUIRED_FILES:
        _check((root / rel).exists(), failures, f"missing required file: {rel}")

    try:
        workflow = _read(root, ".github/workflows/real-ga-readiness.yml")
        for snippet in WORKFLOW_SNIPPETS:
            _check(snippet in workflow, failures, f"real-ga-readiness workflow missing snippet: {snippet}")
        _check("--audit-only" in workflow, failures, "real-ga-readiness workflow must support audit-only candidate runs")
        _check("contents: read" in workflow and "actions: read" in workflow, failures, "real-ga-readiness workflow must use least-privilege read permissions")
    except FileNotFoundError:
        pass

    try:
        manifest = json.loads(_read(root, "release/production-evidence.manifest.json"))
        external = manifest.get("external_evidence_required") or {}
        for key in MANIFEST_REQUIRED_KEYS:
            _check(key in external, failures, f"production evidence manifest missing external requirement: {key}")
        _check(manifest.get("status") == "source_gate_ready_external_evidence_blocked", failures, "manifest must remain external-evidence blocked until real evidence passes")
        _check("main_verification_live_artifact" in manifest, failures, "manifest must declare live main verification artifact gate")
    except Exception as exc:
        failures.append(f"could not validate production evidence manifest: {exc}")

    try:
        external_check = _read(root, "scripts/check_external_ga_evidence.py")
        for snippet in EXTERNAL_CHECK_SNIPPETS:
            _check(snippet in external_check, failures, f"external evidence checker missing snippet: {snippet}")
    except FileNotFoundError:
        pass

    try:
        release_policy = _read(root, ".github/workflows/release-policy.yml")
        _check("check_real_ga_readiness_contract.py" in release_policy, failures, "release policy must enforce real GA readiness source contract")
    except FileNotFoundError:
        pass

    try:
        main_verification = _read(root, ".github/workflows/main-verification.yml")
        _check("check_real_ga_readiness_contract.py" in main_verification, failures, "main verification must enforce real GA readiness source contract")
    except FileNotFoundError:
        pass

    return {
        "schema": "omnidesk-real-ga-readiness-contract/v2",
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "boundary": "This source contract verifies that real-GA collection and validation gates exist. It does not fabricate or replace external evidence.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Real GA readiness source contracts.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--write-report", default="")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    report = audit(root)
    if args.write_report:
        out = Path(args.write_report)
        if not out.is_absolute():
            out = root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "failure_count": len(report["failures"])}, ensure_ascii=False, sort_keys=True))
    for failure in report["failures"]:
        print(f"BLOCKER {failure}", file=sys.stderr)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
