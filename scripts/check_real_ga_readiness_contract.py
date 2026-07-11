#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REQUIRED_FILES = (
    ".github/workflows/real-ga-readiness.yml",
    ".github/workflows/remote-evidence-pipeline.yml",
    ".github/workflows/real-ga-evidence-control-plane.yml",
    ".github/workflows/bigseller-real-contract.yml",
    ".github/workflows/team-governance.yml",
    "scripts/check_live_branch_protection_contract.py",
    "scripts/check_main_verification_artifact_live.py",
    "scripts/check_model_live_smoke_evidence.py",
    "scripts/check_external_ga_evidence.py",
    "scripts/check_real_ga_complete.py",
    "scripts/check_customer_distribution_ga.py",
    "scripts/check_current_release_artifact_binding.py",
    "scripts/check_github_team_governance_live.py",
    "scripts/check_team_governance_contract.py",
    "scripts/import_external_ga_evidence.py",
    "scripts/assemble_external_ga_evidence_bundle.py",
    "scripts/check_bigseller_route_enablement.py",
    "scripts/run_bigseller_live_smoke.py",
    "scripts/import_bigseller_live_smoke_evidence.py",
    "docs/REAL_GA_EVIDENCE_RUNBOOK_1.12.7.md",
)

WORKFLOW_SNIPPETS = (
    "name: Real GA Readiness",
    "check_live_branch_protection_contract.py",
    "check_main_verification_artifact_live.py",
    "check_customer_distribution_ga.py",
    "check_real_ga_complete.py",
    "check_model_live_smoke_evidence.py",
    "check_github_team_governance_live.py",
    "github-team-governance-live.json",
    "readiness_channel",
    "external_evidence_run_id",
    "external_evidence_artifact_name",
    "external-ga-evidence-bound",
    "gh run download",
    "real-ga",
    "candidate",
)

REMOTE_EVIDENCE_WORKFLOW_SNIPPETS = (
    "name: Remote Evidence Pipeline",
    "raw_evidence_run_id",
    "raw_evidence_artifact_name",
    "check_release_configuration.py",
    "--scope external-ga-evidence",
    "import_external_ga_evidence.py",
    "check_real_ga_complete.py",
    "external-ga-evidence",
)

CONTROL_PLANE_WORKFLOW_SNIPPETS = (
    "name: Real GA Evidence Control Plane",
    "browserstack_evidence_run_id",
    "aws_device_farm_evidence_run_id",
    "staging_operations_evidence_run_id",
    "Kubernetes/systemd",
    "assemble_external_ga_evidence_bundle.py",
    "import_external_ga_evidence.py",
    "check_real_ga_complete.py",
    "real-ga-readiness.yml",
    "external-ga-evidence",
)

BIGSELLER_WORKFLOW_SNIPPETS = (
    "name: BigSeller Real Contract",
    "check_bigseller_connector_contract.py",
    "check_bigseller_route_enablement.py",
    "run_bigseller_live_smoke.py",
    "BIGSELLER_REGISTER_ROUTES",
    "BIGSELLER_USE_MOCK",
    "readiness_channel",
    "real-ga",
    "candidate",
)

MANIFEST_REQUIRED_KEYS = (
    "live_branch_protection_control_plane",
    "team_governance_control_plane",
    "native_signed_artifact_bindings",
    "current_release_artifact_binding",
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

COMPLETE_GA_CHECK_SNIPPETS = (
    "team_governance_control_plane",
    "native_signed_artifact_bindings",
    "github-team-governance-live.json",
    "native-signed-artifact-binding.json",
    "artifact_digest_bindings",
    "release_payload_artifact_sha256",
    "external_evidence_signed_artifact_sha256",
    "native_signed_binding_sha256",
    '"artifacts"',
    "check_external_ga_evidence",
)

CUSTOMER_GA_CHECK_SNIPPETS = (
    "audit_complete_real_ga",
    "audit_live_main_verification",
    "main_verification_live_artifact",
    "current_release_artifact_binding",
    "omnidesk-current-release-artifact-binding/v1",
    "omnidesk-customer-distribution-ga/v1",
    "blocked_missing_external_evidence",
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
        _check("workflow_call" in workflow, failures, "real-ga-readiness workflow must support control-plane workflow_call reuse")
        _check("contents: read" in workflow and "actions: read" in workflow, failures, "real-ga-readiness workflow must use least-privilege read permissions")
        _check("--write-live-report" in workflow, failures, "real-ga-readiness workflow must persist the exact-commit Main Verification live report")
        _check("github-team-governance-live.json" in workflow, failures, "real-ga-readiness workflow must persist live team governance evidence")
        _check("pre-release-external-ga-evidence-audit.json" in workflow, failures, "real-ga-readiness must enforce complete pre-release external evidence separately from the Release-only final gate")
    except FileNotFoundError:
        pass

    try:
        remote_evidence_workflow = _read(root, ".github/workflows/remote-evidence-pipeline.yml")
        for snippet in REMOTE_EVIDENCE_WORKFLOW_SNIPPETS:
            _check(snippet in remote_evidence_workflow, failures, f"remote-evidence-pipeline workflow missing snippet: {snippet}")
        _check("contents: read" in remote_evidence_workflow and "actions: read" in remote_evidence_workflow, failures, "remote-evidence-pipeline workflow must use least-privilege read permissions")
    except FileNotFoundError:
        pass

    try:
        control_plane_workflow = _read(root, ".github/workflows/real-ga-evidence-control-plane.yml")
        for snippet in CONTROL_PLANE_WORKFLOW_SNIPPETS:
            _check(snippet in control_plane_workflow, failures, f"real-ga-evidence-control-plane workflow missing snippet: {snippet}")
        _check("contents: read" in control_plane_workflow and "actions: read" in control_plane_workflow, failures, "real-ga-evidence-control-plane workflow must use least-privilege read permissions")
    except FileNotFoundError:
        pass

    try:
        bigseller_workflow = _read(root, ".github/workflows/bigseller-real-contract.yml")
        for snippet in BIGSELLER_WORKFLOW_SNIPPETS:
            _check(snippet in bigseller_workflow, failures, f"bigseller-real-contract workflow missing snippet: {snippet}")
        _check("contents: read" in bigseller_workflow, failures, "bigseller-real-contract workflow must use least-privilege read permissions")
    except FileNotFoundError:
        pass

    try:
        manifest = json.loads(_read(root, "release/production-evidence.manifest.json"))
        external = manifest.get("external_evidence_required") or {}
        for key in MANIFEST_REQUIRED_KEYS:
            _check(key in external, failures, f"production evidence manifest missing external requirement: {key}")
        _check(manifest.get("status") == "source_gate_ready_external_evidence_blocked", failures, "manifest must remain external-evidence blocked until real evidence passes")
        _check("team_governance_contract" in manifest, failures, "manifest must declare team governance contract")
        _check("native_signed_artifact_binding" in manifest, failures, "manifest must declare native signed artifact binding gate")
        native_binding = manifest.get("native_signed_artifact_binding") or {}
        _check(
            native_binding.get("digest_equality_policy")
            == "release_payload_artifact_sha256 == external_evidence_signed_artifact_sha256 == native_signed_binding_sha256",
            failures,
            "native signed artifact binding must declare final artifact digest equality",
        )
        _check(
            "artifact attestation" in str(native_binding.get("platform_identity_policy") or ""),
            failures,
            "native signed artifact binding must bind platform identity and artifact attestation",
        )
        _check("main_verification_live_artifact" in manifest, failures, "manifest must declare live main verification artifact gate")
        _check("bigseller_route_enablement" in manifest, failures, "manifest must declare BigSeller route enablement gate")
        _check("bigseller_real_contract_workflow" in manifest, failures, "manifest must declare BigSeller real contract workflow")
    except Exception as exc:
        failures.append(f"could not validate production evidence manifest: {exc}")

    try:
        complete_check = _read(root, "scripts/check_real_ga_complete.py")
        for snippet in COMPLETE_GA_CHECK_SNIPPETS:
            _check(snippet in complete_check, failures, f"complete Real GA checker missing snippet: {snippet}")
    except FileNotFoundError:
        pass

    try:
        customer_check = _read(root, "scripts/check_customer_distribution_ga.py")
        for snippet in CUSTOMER_GA_CHECK_SNIPPETS:
            _check(snippet in customer_check, failures, f"Customer GA checker missing snippet: {snippet}")
    except FileNotFoundError:
        pass

    try:
        release_policy = _read(root, ".github/workflows/release-policy.yml")
        _check("check_real_ga_readiness_contract.py" in release_policy, failures, "release policy must enforce real GA readiness source contract")
        _check("check_team_governance_contract.py" in release_policy, failures, "release policy must enforce team governance source contract")
        _check("check_bigseller_route_enablement.py" in release_policy, failures, "release policy must enforce BigSeller route enablement source contract")
        _check("check_real_ga_complete.py" in release_policy, failures, "release policy must enforce complete Real GA evidence contract")
    except FileNotFoundError:
        pass

    try:
        release_workflow = _read(root, ".github/workflows/release.yml")
        _check("check_customer_distribution_ga.py" in release_workflow, failures, "release workflow must enforce the final Customer GA boundary")
        _check("external-ga-evidence-bound" in release_workflow, failures, "release workflow must consume the Main Verification-bound external evidence bundle")
        _check("actions/download-artifact@" in release_workflow, failures, "release workflow must download native application artifacts before final signing")
        current_binding_index = release_workflow.find("check_current_release_artifact_binding.py")
        customer_ga_index = release_workflow.find("check_customer_distribution_ga.py")
        final_signing_index = release_workflow.find("python scripts/sign_release.py dist")
        _check(current_binding_index >= 0, failures, "release workflow must rehash and bind current native artifacts")
        _check(
            0 <= current_binding_index < customer_ga_index < final_signing_index,
            failures,
            "current Release artifact binding must run before Customer GA and final release-payload signing",
        )
    except FileNotFoundError:
        pass

    try:
        main_verification = _read(root, ".github/workflows/main-verification.yml")
        _check("check_real_ga_readiness_contract.py" in main_verification, failures, "main verification must enforce real GA readiness source contract")
        _check("native-signed-artifact-binding.json" in main_verification, failures, "main verification must emit native signed artifact binding evidence")
        _check("external-ga-evidence-bound" in main_verification, failures, "main verification must publish a complete bound external evidence bundle")
        _check("check_github_team_governance_live.py" in main_verification, failures, "main verification must refresh live team governance evidence")
        _check('"artifacts": artifact_bindings' in _read(root, "scripts/write_main_verification_evidence.py"), failures, "main verification must emit per-artifact digest bindings")
    except FileNotFoundError:
        pass

    return {
        "schema": "omnidesk-real-ga-readiness-contract/v6",
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "boundary": "This source contract verifies that complete real-GA collection, binding, live-artifact, and final Customer GA validation gates exist. It does not fabricate or replace external evidence.",
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
