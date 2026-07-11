#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
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

PLATFORM_ARTIFACT_BINDINGS = (
    {
        "platform": "android",
        "native": "native-build/flutter-android-release.json",
        "signed": "signed-artifacts/android-signed-aab.json",
        "required_signature_fields": ("android_signer_certificate_sha256",),
        "required_true_fields": ("signature_verified",),
    },
    {
        "platform": "ios",
        "native": "native-build/flutter-ios-release.json",
        "signed": "signed-artifacts/ios-signed-ipa.json",
        "required_signature_fields": ("apple_team_id", "provisioning_profile_uuid", "ipa_codesign_identifier"),
        "required_true_fields": ("signature_verified",),
    },
    {
        "platform": "macos",
        "native": "native-build/tauri-desktop-release.json",
        "signed": "signed-artifacts/desktop-macos-notarized.json",
        "required_signature_fields": ("developer_id_application", "notarization_submission_id"),
        "required_true_fields": ("signature_verified", "notarization_verified"),
    },
    {
        "platform": "windows",
        "native": "native-build/tauri-desktop-release.json",
        "signed": "signed-artifacts/desktop-windows-signed.json",
        "required_signature_fields": ("authenticode_signer", "authenticode_certificate_sha256"),
        "required_true_fields": ("signature_verified", "authenticode_verified"),
    },
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


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _normalize_sha256(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        digest = text[7:]
    else:
        digest = text
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return ""
    return f"sha256:{digest}"


def _platform_value(doc: dict[str, Any], field: str, platform: str) -> Any:
    by_platform = doc.get(f"{field}_by_platform")
    if isinstance(by_platform, dict) and platform in by_platform:
        return by_platform[platform]
    return doc.get(field)


def _artifact_digest(doc: dict[str, Any], *, field: str, platform: str) -> str:
    value = _normalize_sha256(_platform_value(doc, field, platform))
    if value:
        return value
    artifacts = doc.get("artifacts")
    if not isinstance(artifacts, list):
        return ""
    candidates: list[str] = []
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        item_platform = str(item.get("platform") or "").strip().lower()
        if item_platform and item_platform != platform:
            continue
        digest = _normalize_sha256(item.get("sha256"))
        if digest:
            candidates.append(digest)
    return candidates[0] if len(set(candidates)) == 1 else ""


def _artifact_entries(doc: dict[str, Any], *, field: str, platform: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    doc_platform = str(doc.get("platform") or "").strip().lower()
    by_platform = doc.get(f"{field}_by_platform")
    has_platform_digest = isinstance(by_platform, dict) and platform in by_platform
    artifacts = doc.get("artifacts")
    if isinstance(artifacts, list):
        for item in artifacts:
            if not isinstance(item, dict):
                continue
            item_platform = str(item.get("platform") or "").strip().lower()
            aliases = {platform, f"mobile-{platform}", f"desktop-{platform}", f"flutter-{platform}", f"tauri-{platform}"}
            if item_platform and item_platform not in aliases:
                continue
            if not item_platform and doc_platform and doc_platform not in aliases:
                continue
            if not item_platform and not doc_platform and has_platform_digest:
                continue
            digest = _normalize_sha256(item.get(field) or item.get("sha256"))
            if digest:
                entries.append({"path": str(item.get("path") or "").strip() or None, "sha256": digest, "raw": item})
    if entries:
        return entries
    digest = _artifact_digest(doc, field=field, platform=platform)
    return [{"path": str(doc.get("signed_artifact") or "").strip() or None, "sha256": digest, "raw": {}}] if digest else []


def _artifact_attestation(doc: dict[str, Any], item: dict[str, Any], digest: str) -> dict[str, Any] | None:
    candidate = item.get("artifact_attestation")
    if isinstance(candidate, dict):
        return candidate
    candidates = doc.get("artifact_attestations")
    if isinstance(candidates, list):
        for value in candidates:
            if isinstance(value, dict) and _normalize_sha256(value.get("subject_sha256")) == digest:
                return value
    candidate = doc.get("artifact_attestation")
    return candidate if isinstance(candidate, dict) else None


def _artifact_digest_bindings(root: Path, *, commit: str, main_verification_run_id: str) -> list[dict[str, Any]]:
    bindings: list[dict[str, Any]] = []
    for spec in PLATFORM_ARTIFACT_BINDINGS:
        platform = str(spec["platform"])
        native_path = str(spec["native"])
        signed_path = str(spec["signed"])
        native = _load_json(root / native_path)
        signed = _load_json(root / signed_path)
        native_commit = str(native.get("source_commit") or "").strip()
        signed_commit = str(signed.get("source_commit") or "").strip()
        build_run_id = str(native.get("build_run_id") or "").strip()
        signing_run_id = str(signed.get("signing_run_id") or "").strip()
        signature_metadata = {
            field: signed.get(field)
            for field in spec["required_signature_fields"]
        }
        missing_signature_fields = [
            field
            for field, value in signature_metadata.items()
            if not str(value or "").strip()
        ]
        false_verifications = [
            field
            for field in spec["required_true_fields"]
            if signed.get(field) is not True
        ]
        native_entries = _artifact_entries(native, field="release_payload_artifact_sha256", platform=platform)
        signed_entries = _artifact_entries(signed, field="signed_artifact_sha256", platform=platform)
        native_by_digest: dict[str, list[dict[str, Any]]] = {}
        signed_by_digest: dict[str, list[dict[str, Any]]] = {}
        for item in native_entries:
            native_by_digest.setdefault(str(item["sha256"]), []).append(item)
        for item in signed_entries:
            signed_by_digest.setdefault(str(item["sha256"]), []).append(item)
        native_digest_counts = Counter(str(item["sha256"]) for item in native_entries)
        signed_digest_counts = Counter(str(item["sha256"]) for item in signed_entries)
        digest_sets_match = bool(native_digest_counts) and native_digest_counts == signed_digest_counts
        artifact_bindings: list[dict[str, Any]] = []
        for digest in sorted(set(native_digest_counts) | set(signed_digest_counts)):
            for index in range(max(native_digest_counts[digest], signed_digest_counts[digest])):
                native_item = native_by_digest.get(digest, [])[index] if index < native_digest_counts[digest] else {}
                signed_item = signed_by_digest.get(digest, [])[index] if index < signed_digest_counts[digest] else {}
                signed_raw = signed_item.get("raw") if isinstance(signed_item.get("raw"), dict) else {}
                binding_digest = _normalize_sha256(signed_raw.get("native_signed_binding_sha256")) or _normalize_sha256(
                    signed.get("native_signed_binding_sha256")
                )
                attestation = _artifact_attestation(signed, signed_raw, digest)
                attestation_digest = _normalize_sha256(attestation.get("subject_sha256")) if isinstance(attestation, dict) else ""
                attestation_id = str(attestation.get("attestation_id") or "").strip() if isinstance(attestation, dict) else ""
                artifact_valid = all(
                    (
                        bool(native_item),
                        bool(signed_item),
                        digest == binding_digest,
                        bool(attestation_id),
                        attestation_digest == digest,
                    )
                )
                artifact_bindings.append(
                    {
                        "path": signed_item.get("path") or native_item.get("path"),
                        "native_build_artifact_path": native_item.get("path"),
                        "signed_artifact_path": signed_item.get("path"),
                        "release_payload_artifact_sha256": digest if native_item else None,
                        "external_evidence_signed_artifact_sha256": digest if signed_item else None,
                        "native_signed_binding_sha256": binding_digest or None,
                        "artifact_attestation": attestation,
                        "source_commit": commit,
                        "build_run_id": build_run_id or None,
                        "signing_run_id": signing_run_id or None,
                        "main_verification_run_id": main_verification_run_id,
                        "valid": artifact_valid,
                    }
                )
        valid = all(
            (
                bool(native),
                bool(signed),
                digest_sets_match,
                bool(artifact_bindings),
                all(item["valid"] for item in artifact_bindings),
                native_commit == commit,
                signed_commit == commit,
                bool(build_run_id),
                bool(signing_run_id),
                not missing_signature_fields,
                not false_verifications,
            )
        )
        single = artifact_bindings[0] if len(artifact_bindings) == 1 else {}
        bindings.append(
            {
                "platform": platform,
                "native_build_evidence_path": native_path,
                "signed_artifact_evidence_path": signed_path,
                "release_payload_artifact_sha256": single.get("release_payload_artifact_sha256"),
                "external_evidence_signed_artifact_sha256": single.get("external_evidence_signed_artifact_sha256"),
                "native_signed_binding_sha256": single.get("native_signed_binding_sha256"),
                "digests_match": digest_sets_match and all(item["valid"] for item in artifact_bindings),
                "source_commit": commit,
                "native_source_commit": native_commit or None,
                "signed_source_commit": signed_commit or None,
                "build_run_id": build_run_id or None,
                "signing_run_id": signing_run_id or None,
                "artifact_attestation": single.get("artifact_attestation"),
                "artifacts": artifact_bindings,
                "main_verification_run_id": main_verification_run_id,
                "signature_metadata": signature_metadata,
                "missing_signature_fields": missing_signature_fields,
                "failed_verifications": false_verifications,
                "valid": valid,
            }
        )
    return bindings


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

    commit = _env("GITHUB_SHA", "local")
    run_id = _env("GITHUB_RUN_ID", "local")
    run_attempt = _env("GITHUB_RUN_ATTEMPT", "1")
    workflow = _env("GITHUB_WORKFLOW", "Main Verification")
    ref = _env("GITHUB_REF", "local")
    repository = _env("GITHUB_REPOSITORY", "local")
    trigger = _env("GITHUB_EVENT_NAME", "local")
    artifact_digest_bindings = _artifact_digest_bindings(
        external_evidence_root,
        commit=commit,
        main_verification_run_id=run_id,
    )
    all_artifact_digest_bindings_valid = all(row["valid"] for row in artifact_digest_bindings)
    external_evidence_complete = (
        all_native_present
        and all_signed_present
        and prebinding_audit_passed
        and all_artifact_digest_bindings_valid
    )
    customer_ga_status = "passed" if external_evidence_complete else "blocked_missing_external_evidence"

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
        "all_artifact_digest_bindings_valid": all_artifact_digest_bindings_valid,
        "artifact_digest_bindings": artifact_digest_bindings,
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
        "all_artifact_digest_bindings_valid": all_artifact_digest_bindings_valid,
        "artifact_digest_bindings": artifact_digest_bindings,
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
        "all_artifact_digest_bindings_valid": all_artifact_digest_bindings_valid,
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
