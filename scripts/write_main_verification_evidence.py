#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable

SIGNED_REQUIRED = (
    "signed-artifacts/android-signed-aab.json",
    "signed-artifacts/ios-signed-ipa.json",
    "signed-artifacts/desktop-macos-notarized.json",
    "signed-artifacts/desktop-windows-signed.json",
)

NATIVE_REQUIRED = (
    "native-build/flutter-android-release.json",
    "native-build/flutter-ios-release.json",
    "native-build/tauri-desktop-release.json",
    "native-build/rust-cargo-check-locked.json",
)

REQUIRED_GATES = (
    "version_consistency",
    "release_channel_policy",
    "ci_evidence_contract",
    "security_workflow_policy",
    "security_attack_surface",
    "team_governance_contract",
    "real_ga_readiness_contract",
    "bigseller_connector_contract",
    "enterprise_dependency_contract",
    "observability_contract",
    "deployment_readiness",
    "supply_chain_standard",
    "enterprise_readiness",
    "kubernetes_contract",
    "branch_protection_contract",
    "complete_real_ga_prebinding_audit",
    "complete_real_ga_evidence_contract",
    "focused_runtime_regression",
    "web_admin_verify",
    "desktop_verify",
    "native_signed_artifact_binding",
)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _collect(root: Path, rel_paths: Iterable[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rel in rel_paths:
        path = root / rel
        present = path.is_file()
        rows.append(
            {
                "path": rel,
                "present": present,
                "sha256": _sha256(path) if present else None,
            }
        )
    return rows


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_audit_status(path: Path | None) -> tuple[str, int | None]:
    if path is None or not path.is_file():
        return "not_supplied", None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "invalid", None
    status = str(report.get("status") or "invalid").strip()
    blocker_count = report.get("blocker_count")
    return status, int(blocker_count) if isinstance(blocker_count, int) else None


def write_evidence(
    *,
    output_dir: Path,
    external_evidence_root: Path,
    real_ga_summary: str,
    real_ga_audit_report: Path | None = None,
) -> dict[str, dict[str, Any]]:
    native_rows = _collect(external_evidence_root, NATIVE_REQUIRED)
    signed_rows = _collect(external_evidence_root, SIGNED_REQUIRED)
    all_native_present = all(row["present"] for row in native_rows)
    all_signed_present = all(row["present"] for row in signed_rows)
    audit_status, audit_blocker_count = _read_audit_status(real_ga_audit_report)
    prebinding_audit_passed = audit_status == "passed"
    external_evidence_complete = all_native_present and all_signed_present and prebinding_audit_passed
    customer_ga_status = "passed" if external_evidence_complete else "blocked_missing_external_evidence"

    commit = _env("GITHUB_SHA", "local")
    run_id = _env("GITHUB_RUN_ID", "local")
    run_attempt = _env("GITHUB_RUN_ATTEMPT", "1")
    workflow = _env("GITHUB_WORKFLOW", "Main Verification")
    ref = _env("GITHUB_REF", "local")
    repository = _env("GITHUB_REPOSITORY", "local")
    trigger = _env("GITHUB_EVENT_NAME", "local")

    evidence = {
        "schema": "omnidesk-main-verification/v1",
        "status": customer_ga_status,
        "source_verification_status": "passed",
        "customer_distribution_ga_status": customer_ga_status,
        "real_ga_prebinding_audit_status": audit_status,
        "real_ga_prebinding_audit_blocker_count": audit_blocker_count,
        "commit": commit,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "workflow": workflow,
        "ref": ref,
        "repository": repository,
        "trigger": trigger,
        "required_gates": list(REQUIRED_GATES),
        "gate_results": {
            "source_verification": "passed",
            "complete_real_ga_prebinding_audit": audit_status,
            "native_signed_artifact_binding": customer_ga_status,
        },
        "native_build_evidence": native_rows,
        "signed_artifact_evidence": signed_rows,
        "all_required_native_builds_present": all_native_present,
        "all_required_signed_artifacts_present": all_signed_present,
    }

    evidence_path = output_dir / "main-verification-evidence.json"
    _write_json(evidence_path, evidence)
    evidence_digest = _sha256(evidence_path)

    artifact_name = f"main-verification-evidence-{commit}"
    binding = {
        "schema": "omnidesk-native-signed-artifact-binding/v1",
        "status": customer_ga_status,
        "produced_at": _env("GITHUB_RUN_STARTED_AT", "workflow-run"),
        "producer": "Main Verification",
        "repository": repository,
        "main_verification_commit": commit,
        "main_verification_run_id": run_id,
        "main_verification_artifact_name": artifact_name,
        "main_verification_evidence_digest": evidence_digest,
        "real_ga_evidence_summary": real_ga_summary,
        "real_ga_prebinding_audit_status": audit_status,
        "native_builds_bound": all_native_present and prebinding_audit_passed,
        "signed_artifacts_bound": all_signed_present and prebinding_audit_passed,
        "all_required_native_builds_present": all_native_present,
        "all_required_signed_artifacts_present": all_signed_present,
        "native_build_evidence_paths": list(NATIVE_REQUIRED),
        "signed_artifact_evidence_paths": list(SIGNED_REQUIRED),
        "native_build_evidence": native_rows,
        "signed_artifact_evidence": signed_rows,
        "policy": (
            "Customer-distribution Real GA requires the complete pre-binding audit to pass, "
            "then all native build and signed artifact evidence files to be present, hashed, "
            "and bound into this commit-addressed artifact."
        ),
    }
    binding_path = output_dir / "native-signed-artifact-binding.json"
    _write_json(binding_path, binding)

    manifest = {
        "schema": "omnidesk-main-verification-artifact/v1",
        "status": customer_ga_status,
        "source_verification_status": "passed",
        "customer_distribution_ga_status": customer_ga_status,
        "real_ga_prebinding_audit_status": audit_status,
        "commit": commit,
        "run_id": run_id,
        "artifact_name": artifact_name,
        "evidence_path": evidence_path.name,
        "evidence_digest": evidence_digest,
        "native_signed_artifact_binding_path": binding_path.name,
        "native_signed_artifact_binding_status": binding["status"],
        "all_required_native_builds_present": all_native_present,
        "all_required_signed_artifacts_present": all_signed_present,
    }
    manifest_path = output_dir / "main-verification-artifact.json"
    _write_json(manifest_path, manifest)

    control_plane_dir = external_evidence_root / "control-plane"
    control_plane_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(evidence_path, control_plane_dir / evidence_path.name)
    shutil.copy2(binding_path, control_plane_dir / binding_path.name)

    return {"evidence": evidence, "binding": binding, "manifest": manifest}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write truthful main-verification evidence and optionally require complete external Real GA evidence."
    )
    parser.add_argument("--output-dir", default="dist/evidence")
    parser.add_argument("--external-evidence-root", default="release/external-evidence")
    parser.add_argument("--real-ga-summary", default="release/real-ga-evidence-summary-1.12.7.json")
    parser.add_argument("--real-ga-audit-report", default="")
    parser.add_argument("--require-external-evidence", action="store_true")
    args = parser.parse_args(argv)

    audit_report = Path(args.real_ga_audit_report) if args.real_ga_audit_report else None
    artifacts = write_evidence(
        output_dir=Path(args.output_dir),
        external_evidence_root=Path(args.external_evidence_root),
        real_ga_summary=args.real_ga_summary,
        real_ga_audit_report=audit_report,
    )
    status = artifacts["manifest"]["status"]
    print(f"MAIN_VERIFICATION_STATUS={status}")
    print(f"MAIN_VERIFICATION_EVIDENCE_DIGEST={artifacts['manifest']['evidence_digest']}")
    print(f"NATIVE_SIGNED_ARTIFACT_BINDING_STATUS={artifacts['binding']['status']}")
    print(f"REAL_GA_PREBINDING_AUDIT_STATUS={artifacts['manifest']['real_ga_prebinding_audit_status']}")

    if args.require_external_evidence and status != "passed":
        missing = [
            row["path"]
            for key in ("native_build_evidence", "signed_artifact_evidence")
            for row in artifacts["evidence"][key]
            if not row["present"]
        ]
        print("external Real GA evidence is incomplete or failed semantic audit:", file=sys.stderr)
        print(
            f"  - pre-binding audit status: {artifacts['manifest']['real_ga_prebinding_audit_status']}",
            file=sys.stderr,
        )
        for path in missing:
            print(f"  - missing: {path}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
