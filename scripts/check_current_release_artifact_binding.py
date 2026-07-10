#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PLATFORM_SPECS = (
    {
        "platform": "android",
        "artifact_directory": "release-mobile-android",
        "extensions": (".aab",),
        "signed_evidence": "signed-artifacts/android-signed-aab.json",
        "signature_fields": ("android_signer_certificate_sha256",),
        "true_fields": ("signature_verified",),
    },
    {
        "platform": "ios",
        "artifact_directory": "release-mobile-ios",
        "extensions": (".ipa",),
        "signed_evidence": "signed-artifacts/ios-signed-ipa.json",
        "signature_fields": ("apple_team_id", "provisioning_profile_uuid", "ipa_codesign_identifier"),
        "true_fields": ("signature_verified",),
    },
    {
        "platform": "macos",
        "artifact_directory": "release-desktop-macos",
        "extensions": (".dmg", ".pkg"),
        "signed_evidence": "signed-artifacts/desktop-macos-notarized.json",
        "signature_fields": ("developer_id_application", "notarization_submission_id"),
        "true_fields": ("signature_verified", "notarization_verified"),
    },
    {
        "platform": "windows",
        "artifact_directory": "release-desktop-windows",
        "extensions": (".msi", ".exe", ".msix"),
        "signed_evidence": "signed-artifacts/desktop-windows-signed.json",
        "signature_fields": ("authenticode_signer", "authenticode_certificate_sha256"),
        "true_fields": ("signature_verified", "authenticode_verified"),
    },
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def _normalize_sha256(value: Any) -> str:
    text = str(value or "").strip().lower()
    digest = text[7:] if text.startswith("sha256:") else text
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return ""
    return f"sha256:{digest}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _artifact_rows(doc: dict[str, Any], *, platform: str, digest_field: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    artifacts = doc.get("artifacts")
    if isinstance(artifacts, list):
        for item in artifacts:
            if not isinstance(item, dict):
                continue
            item_platform = str(item.get("platform") or "").strip().lower()
            if item_platform and item_platform != platform:
                continue
            digest = _normalize_sha256(item.get(digest_field) or item.get("sha256"))
            if digest:
                rows.append({**item, "sha256": digest})
    if rows:
        return rows
    digest = _normalize_sha256(doc.get(digest_field))
    if digest:
        return [{"path": doc.get("signed_artifact"), "sha256": digest}]
    by_platform = doc.get(f"{digest_field}_by_platform")
    if isinstance(by_platform, dict):
        digest = _normalize_sha256(by_platform.get(platform))
        if digest:
            return [{"path": None, "platform": platform, "sha256": digest}]
    return []


def _attestation_for(doc: dict[str, Any], item: dict[str, Any], digest: str) -> dict[str, Any] | None:
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


def _main_binding_rows(row: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = row.get("artifacts")
    return [item for item in artifacts if isinstance(item, dict)] if isinstance(artifacts, list) else []


def audit(
    *,
    artifact_root: Path,
    evidence_root: Path,
    repository: str,
    source_commit: str,
    release_run_id: str,
    main_verification_run_id: str,
) -> dict[str, Any]:
    failures: list[str] = []
    platform_reports: list[dict[str, Any]] = []
    main_binding_path = evidence_root / "control-plane/native-signed-artifact-binding.json"
    try:
        main_binding = _read_json(main_binding_path)
    except (OSError, ValueError, RuntimeError) as exc:
        main_binding = {}
        failures.append(f"cannot read Main Verification native binding: {exc}")
    main_rows = {
        str(item.get("platform") or "").strip().lower(): item
        for item in main_binding.get("artifact_digest_bindings") or []
        if isinstance(item, dict)
    }
    if main_binding.get("status") != "passed":
        failures.append("Main Verification native signed artifact binding must have status passed")
    if str(main_binding.get("repository") or "") != repository:
        failures.append("Main Verification native binding repository does not match this release")
    if str(main_binding.get("main_verification_commit") or "") != source_commit:
        failures.append("Main Verification native binding commit does not match this release")
    if str(main_binding.get("main_verification_run_id") or "") != main_verification_run_id:
        failures.append("Main Verification native binding run id does not match the selected evidence run")

    for spec in PLATFORM_SPECS:
        platform = str(spec["platform"])
        platform_failures: list[str] = []
        platform_root = artifact_root / str(spec["artifact_directory"])
        manifest_path = platform_root / "native-artifact-manifest.json"
        try:
            manifest = _read_json(manifest_path)
        except (OSError, ValueError, RuntimeError) as exc:
            manifest = {}
            platform_failures.append(f"cannot read current release manifest: {exc}")
        if manifest.get("schema") != "omnidesk-native-artifact-set/v1" or manifest.get("status") != "passed":
            platform_failures.append("current release native artifact manifest must be a passed v1 manifest")
        if str(manifest.get("source_commit") or "") != source_commit:
            platform_failures.append("current release manifest source_commit does not match")
        if str(manifest.get("build_run_id") or "") != release_run_id:
            platform_failures.append("current release manifest build_run_id does not match this Release Build run")

        selected_manifest_rows: list[dict[str, Any]] = []
        for item in manifest.get("artifacts") or []:
            if not isinstance(item, dict):
                continue
            rel = str(item.get("path") or "").strip()
            if rel.lower().endswith(tuple(spec["extensions"])):
                selected_manifest_rows.append(item)
        if not selected_manifest_rows:
            platform_failures.append(
                f"current release contains no distributable {platform} artifact with extensions {list(spec['extensions'])}"
            )

        payload_root = (platform_root / "payload").resolve()
        payload_paths = {
            path.relative_to(payload_root).as_posix()
            for path in payload_root.rglob("*")
            if path.is_file() and path.name.lower().endswith(tuple(spec["extensions"]))
        } if payload_root.is_dir() else set()
        manifest_paths = {str(item.get("path") or "").strip() for item in selected_manifest_rows}
        if payload_paths != manifest_paths:
            platform_failures.append(
                f"current release distributable files do not exactly match the manifest: unlisted={sorted(payload_paths - manifest_paths)}, missing={sorted(manifest_paths - payload_paths)}"
            )

        actual_rows: list[dict[str, Any]] = []
        for item in selected_manifest_rows:
            rel = str(item.get("path") or "").strip()
            path = (payload_root / rel).resolve()
            declared = _normalize_sha256(item.get("sha256") or item.get("release_payload_artifact_sha256"))
            safe_path = path.is_relative_to(payload_root)
            actual = _sha256(path) if safe_path and path.is_file() else ""
            valid = bool(actual) and actual == declared
            if not safe_path:
                platform_failures.append(f"current release manifest path escapes payload root: {platform}/{rel}")
            elif not path.is_file():
                platform_failures.append(f"current release artifact is missing: {platform}/{rel}")
            elif not valid:
                platform_failures.append(f"current release manifest digest mismatch: {platform}/{rel}")
            actual_rows.append({"path": rel, "sha256": actual or None, "manifest_sha256": declared or None, "valid": valid})

        signed_path = evidence_root / str(spec["signed_evidence"])
        try:
            signed = _read_json(signed_path)
        except (OSError, ValueError, RuntimeError) as exc:
            signed = {}
            platform_failures.append(f"cannot read signed artifact evidence: {exc}")
        if signed.get("status") != "passed":
            platform_failures.append("external signed artifact evidence must have status passed")
        if str(signed.get("source_commit") or "") != source_commit:
            platform_failures.append("external signed artifact evidence source_commit does not match")
        signing_run_id = str(signed.get("signing_run_id") or "").strip()
        if not signing_run_id:
            platform_failures.append("external signing_run_id is required")
        for field in spec["signature_fields"]:
            if not str(signed.get(field) or "").strip():
                platform_failures.append(f"external signature metadata missing {field}")
        for field in spec["true_fields"]:
            if signed.get(field) is not True:
                platform_failures.append(f"external verification {field} must be true")

        signed_rows = _artifact_rows(signed, platform=platform, digest_field="signed_artifact_sha256")
        signed_digests = sorted(
            digest
            for digest in (_normalize_sha256(item.get("sha256")) for item in signed_rows)
            if digest
        )
        for item in signed_rows:
            digest = _normalize_sha256(item.get("sha256"))
            binding_digest = _normalize_sha256(item.get("native_signed_binding_sha256")) or _normalize_sha256(
                signed.get("native_signed_binding_sha256")
            )
            attestation = _attestation_for(signed, item, digest)
            if digest != binding_digest:
                platform_failures.append(f"external signed/native binding digest mismatch for {platform} {item.get('path')}")
            if not isinstance(attestation, dict) or not str(attestation.get("attestation_id") or "").strip():
                platform_failures.append(f"artifact attestation id is required for {platform} {item.get('path')}")
            elif _normalize_sha256(attestation.get("subject_sha256")) != digest:
                platform_failures.append(f"artifact attestation subject digest mismatch for {platform} {item.get('path')}")
        if not signed_rows:
            platform_failures.append("external signed artifact evidence must enumerate every signed artifact")

        main_row = main_rows.get(platform) or {}
        bound_build_run_id = str(main_row.get("build_run_id") or "").strip()
        bound_signing_run_id = str(main_row.get("signing_run_id") or "").strip()
        if str(main_row.get("source_commit") or "") != source_commit:
            platform_failures.append(f"Main Verification platform source_commit mismatch for {platform}")
        if not bound_build_run_id:
            platform_failures.append(f"Main Verification platform build_run_id is required for {platform}")
        if not bound_signing_run_id or bound_signing_run_id != signing_run_id:
            platform_failures.append(f"Main Verification platform signing_run_id must match external evidence for {platform}")
        signature_metadata = main_row.get("signature_metadata")
        if not isinstance(signature_metadata, dict):
            platform_failures.append(f"Main Verification signature metadata is required for {platform}")
        else:
            for field in spec["signature_fields"]:
                if not str(signature_metadata.get(field) or "").strip():
                    platform_failures.append(f"Main Verification signature metadata missing {field}")
        if main_row.get("missing_signature_fields") not in ([], None):
            platform_failures.append(f"Main Verification reports missing signature fields for {platform}")
        if main_row.get("failed_verifications") not in ([], None):
            platform_failures.append(f"Main Verification reports failed platform verifications for {platform}")
        main_artifacts = _main_binding_rows(main_row)
        main_digests: list[str] = []
        if not main_artifacts:
            platform_failures.append("Main Verification binding must contain per-artifact rows")
        for item in main_artifacts:
            release_digest = _normalize_sha256(item.get("release_payload_artifact_sha256"))
            signed_digest = _normalize_sha256(item.get("external_evidence_signed_artifact_sha256"))
            binding_digest = _normalize_sha256(item.get("native_signed_binding_sha256"))
            if not release_digest or release_digest != signed_digest or signed_digest != binding_digest:
                platform_failures.append(f"Main Verification per-artifact digest chain is invalid for {platform}")
            else:
                main_digests.append(binding_digest)
            attestation = item.get("artifact_attestation")
            if (
                not isinstance(attestation, dict)
                or not str(attestation.get("attestation_id") or "").strip()
                or _normalize_sha256(attestation.get("subject_sha256")) != binding_digest
            ):
                platform_failures.append(f"Main Verification per-artifact attestation is invalid for {platform}")
            if item.get("valid") is not True:
                platform_failures.append(f"Main Verification per-artifact binding is not valid for {platform}")
            if str(item.get("source_commit") or "") != source_commit:
                platform_failures.append(f"Main Verification per-artifact source_commit mismatch for {platform}")
            if str(item.get("build_run_id") or "") != bound_build_run_id:
                platform_failures.append(f"Main Verification per-artifact build_run_id mismatch for {platform}")
            if str(item.get("signing_run_id") or "") != bound_signing_run_id:
                platform_failures.append(f"Main Verification per-artifact signing_run_id mismatch for {platform}")
            if str(item.get("main_verification_run_id") or "") != main_verification_run_id:
                platform_failures.append(f"Main Verification per-artifact run id mismatch for {platform}")
        if main_row.get("valid") is not True:
            platform_failures.append(f"Main Verification platform binding is not valid for {platform}")

        actual_digests = sorted(str(item.get("sha256") or "") for item in actual_rows if item.get("valid"))
        main_digests.sort()
        if not actual_digests or actual_digests != signed_digests or signed_digests != main_digests:
            platform_failures.append(
                f"{platform} digest sets differ across current Release payload, external signed evidence, and Main Verification binding"
            )

        failures.extend(f"{platform}: {failure}" for failure in platform_failures)
        platform_reports.append(
            {
                "platform": platform,
                "status": "passed" if not platform_failures else "blocked",
                "current_release_manifest": str(manifest_path),
                "signed_artifact_evidence": str(signed_path),
                "current_release_build_run_id": manifest.get("build_run_id"),
                "bound_native_build_run_id": bound_build_run_id or None,
                "external_signing_run_id": signing_run_id,
                "main_verification_run_id": main_binding.get("main_verification_run_id"),
                "artifacts": actual_rows,
                "actual_digest_set": actual_digests,
                "external_signed_digest_set": signed_digests,
                "main_verification_digest_set": main_digests,
                "failures": platform_failures,
            }
        )

    return {
        "schema": "omnidesk-current-release-artifact-binding/v1",
        "status": "passed" if not failures else "blocked",
        "produced_at": datetime.now(timezone.utc).isoformat(),
        "producer": "OmniDesk current Release artifact binding checker",
        "repository": repository,
        "source_commit": source_commit,
        "release_run_id": release_run_id,
        "main_verification_run_id": main_verification_run_id,
        "artifact_root": str(artifact_root),
        "evidence_root": str(evidence_root),
        "all_artifacts_bound": not failures,
        "platforms": platform_reports,
        "failures": failures,
        "policy": "Every distributable native file built by this Release run must have the same SHA-256 in the current manifest, external platform-signing evidence, artifact attestation subject, and enforced Main Verification binding before Customer GA and final release-payload signing.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bind current Release-built native files to signed external evidence.")
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--evidence-root", default="release/external-evidence")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--source-commit", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--release-run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    parser.add_argument("--main-verification-run-id", required=True)
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--write-report", default="dist/current-release-artifact-binding.json")
    args = parser.parse_args(argv)

    report = audit(
        artifact_root=Path(args.artifact_root).resolve(),
        evidence_root=Path(args.evidence_root).resolve(),
        repository=args.repository,
        source_commit=args.source_commit,
        release_run_id=args.release_run_id,
        main_verification_run_id=args.main_verification_run_id,
    )
    output = Path(args.write_report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "failure_count": len(report["failures"])}, sort_keys=True))
    for failure in report["failures"]:
        print(f"BLOCKER {failure}", file=sys.stderr)
    return 0 if args.audit_only or report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
