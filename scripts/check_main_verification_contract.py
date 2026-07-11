#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


REQUIRED_WORKFLOW_SNIPPETS = (
    "workflow_dispatch:",
    "pull_request:",
    "push:",
    "branches:",
    "- main",
    "check_security_attack_surface.py .",
    "security_attack_surface",
    "check_main_verification_contract.py .",
    "check_real_ga_prebinding.py .",
    "--real-ga-audit-report",
    "--require-external-evidence",
    "check_real_ga_complete.py .",
    "check_live_branch_protection_contract.py .",
    "check_github_team_governance_live.py .",
    "github-team-governance-live.json",
    "main-verification-evidence-${{ github.sha }}",
    "main-verification-evidence.json",
    "main-verification-artifact.json",
)

REQUIRED_WRITER_SNIPPETS = (
    "hashlib.sha256",
    "omnidesk-main-verification/v1",
    "omnidesk-main-verification-artifact/v1",
    "omnidesk-native-signed-artifact-binding/v1",
    'prebinding_audit_passed = audit_status == "passed"',
    "all_artifact_digest_bindings_valid = all(",
    "and all_artifact_digest_bindings_valid",
    '"release_payload_artifact_sha256"',
    '"external_evidence_signed_artifact_sha256"',
    '"native_signed_binding_sha256"',
    '"artifacts": artifact_bindings',
    '"source_verification_status": "passed"',
    '"customer_distribution_ga_status": customer_ga_status',
    '"real_ga_prebinding_audit_status": audit_status',
    '"status": customer_ga_status',
    '"native_signed_artifact_binding": customer_ga_status',
)

REQUIRED_LIVE_CHECKER_SNIPPETS = (
    "event=workflow_dispatch",
    'str(run.get("event") or "") == "workflow_dispatch"',
    '"required_event": "workflow_dispatch"',
    "complete semantic Real GA audit",
)

REQUIRED_CONTRACT_KEYS = (
    "schema",
    "artifact_manifest_schema",
    "artifact_name_pattern",
    "evidence_file",
    "artifact_manifest_file",
    "evidence_digest_algorithm",
    "required_for_customer_distribution_ga",
)


def _read(path: Path) -> str:
    if not path.exists():
        raise RuntimeError(f"missing required file: {path}")
    return path.read_text(encoding="utf-8")


def _json(path: Path) -> dict:
    return json.loads(_read(path))


def _assert(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def _workflow_input_is_required(workflow: str, input_name: str) -> bool:
    lines = workflow.splitlines()
    target = f"{input_name}:"
    for index, line in enumerate(lines):
        if line.strip() != target:
            continue
        indent = len(line) - len(line.lstrip(" "))
        for child in lines[index + 1 :]:
            stripped = child.strip()
            if not stripped or stripped.startswith("#"):
                continue
            child_indent = len(child) - len(child.lstrip(" "))
            if child_indent <= indent:
                break
            if stripped == "required: true":
                return True
        return False
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the post-merge main verification evidence contract.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    workflow = _read(root / ".github" / "workflows" / "main-verification.yml")
    writer = _read(root / "scripts" / "write_main_verification_evidence.py")
    live_checker = _read(root / "scripts" / "check_main_verification_artifact_live.py")
    evidence_manifest = _json(root / "release" / "production-evidence.manifest.json")
    failures: list[str] = []

    for snippet in REQUIRED_WORKFLOW_SNIPPETS:
        _assert(snippet in workflow, f"main-verification.yml must contain {snippet!r}", failures)
    for snippet in REQUIRED_WRITER_SNIPPETS:
        _assert(snippet in writer, f"write_main_verification_evidence.py must contain {snippet!r}", failures)
    for snippet in REQUIRED_LIVE_CHECKER_SNIPPETS:
        _assert(snippet in live_checker, f"check_main_verification_artifact_live.py must contain {snippet!r}", failures)

    _assert(re.search(r"^on:\s*$", workflow, re.MULTILINE) is not None, "main-verification.yml must declare workflow triggers", failures)
    _assert("cancel-in-progress: false" in workflow, "main verification must not cancel in-progress evidence runs", failures)
    _assert("GITHUB_SHA" in writer and "GITHUB_RUN_ID" in writer and "GITHUB_RUN_ATTEMPT" in writer, "main evidence must bind commit, run id, and run attempt", failures)
    _assert("required_gates" in writer, "main evidence must list the gates that were executed", failures)
    _assert("external_evidence_run_id" in workflow, "workflow must accept an external evidence run id", failures)
    _assert(
        _workflow_input_is_required(workflow, "external_evidence_run_id"),
        "workflow_dispatch must require an external evidence run id",
        failures,
    )
    _assert("if [[ -n \"${EXTERNAL_EVIDENCE_RUN_ID:-}\" ]]" in workflow, "supplied external evidence must enable fail-closed enforcement", failures)
    _assert("--audit-only" in workflow and "external-ga-prebinding-audit.json" in workflow, "workflow must produce a complete pre-binding semantic audit", failures)
    _assert("--evidence-dir release/external-evidence" in workflow, "enforced runs must execute the final complete Real GA audit", failures)
    _assert(
        workflow.find("check_github_team_governance_live.py") < workflow.find("check_real_ga_prebinding.py"),
        "live team governance evidence must be refreshed before the pre-binding Real GA audit",
        failures,
    )

    main_contract = evidence_manifest.get("main_verification_evidence")
    _assert(isinstance(main_contract, dict), "production-evidence manifest must declare main_verification_evidence", failures)
    if isinstance(main_contract, dict):
        for key in REQUIRED_CONTRACT_KEYS:
            _assert(key in main_contract, f"main_verification_evidence must declare {key}", failures)
        _assert(main_contract.get("schema") == "omnidesk-main-verification/v1", "main verification schema must match writer evidence schema", failures)
        _assert(main_contract.get("artifact_manifest_schema") == "omnidesk-main-verification-artifact/v1", "main verification artifact schema must match writer artifact schema", failures)
        _assert(str(main_contract.get("artifact_name_pattern", "")).endswith("-${commit_sha}"), "artifact name must be commit-addressed", failures)
        _assert(main_contract.get("evidence_digest_algorithm") == "sha256", "main verification evidence digest must use sha256", failures)
        _assert(main_contract.get("required_for_customer_distribution_ga") is True, "main verification evidence must be required for customer-distribution GA", failures)

    security_contract = evidence_manifest.get("security_attack_surface_gate")
    _assert(isinstance(security_contract, dict), "production-evidence manifest must declare security_attack_surface_gate", failures)
    if isinstance(security_contract, dict):
        _assert(security_contract.get("schema") == "omnidesk-security-attack-surface/v1", "security attack surface schema must match checker report schema", failures)
        _assert("check_security_attack_surface.py" in str(security_contract.get("contract_check", "")), "security attack surface contract check must be declared", failures)
        _assert(security_contract.get("required_for_customer_distribution_ga") is True, "security attack surface gate must be required for customer-distribution GA", failures)

    if failures:
        print("main verification contract check failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("main verification contract ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
