from __future__ import annotations

import json
from pathlib import Path

from scripts.write_main_verification_evidence import write_evidence


def _set_github_env(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_WORKFLOW", "Main Verification")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/omnidesk")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")


def _write_required_files(root: Path) -> None:
    digests = {
        "android": "sha256:" + "a" * 64,
        "ios": "sha256:" + "b" * 64,
        "macos": "sha256:" + "c" * 64,
        "windows": "sha256:" + "d" * 64,
    }
    native_docs = {
        "native-build/flutter-android-release.json": {
            "status": "passed",
            "source_commit": "abc123",
            "build_run_id": "build-android",
            "release_payload_artifact_sha256": digests["android"],
        },
        "native-build/flutter-ios-release.json": {
            "status": "passed",
            "source_commit": "abc123",
            "build_run_id": "build-ios",
            "release_payload_artifact_sha256": digests["ios"],
        },
        "native-build/tauri-desktop-release.json": {
            "status": "passed",
            "source_commit": "abc123",
            "build_run_id": "build-desktop",
            "release_payload_artifact_sha256_by_platform": {
                "macos": digests["macos"],
                "windows": digests["windows"],
            },
        },
        "native-build/rust-cargo-check-locked.json": {
            "status": "passed",
            "source_commit": "abc123",
            "build_run_id": "build-rust",
        },
    }
    signed_docs = {
        "signed-artifacts/android-signed-aab.json": {
            "platform": "android",
            "status": "passed",
            "source_commit": "abc123",
            "signing_run_id": "sign-android",
            "signature_verified": True,
            "android_signer_certificate_sha256": "sha256:" + "1" * 64,
        },
        "signed-artifacts/ios-signed-ipa.json": {
            "platform": "ios",
            "status": "passed",
            "source_commit": "abc123",
            "signing_run_id": "sign-ios",
            "signature_verified": True,
            "apple_team_id": "TEAM123456",
            "provisioning_profile_uuid": "profile-uuid",
            "ipa_codesign_identifier": "com.omnidesk.mobile",
        },
        "signed-artifacts/desktop-macos-notarized.json": {
            "platform": "macos",
            "status": "passed",
            "source_commit": "abc123",
            "signing_run_id": "sign-macos",
            "signature_verified": True,
            "notarization_verified": True,
            "developer_id_application": "Developer ID Application: OmniDesk",
            "notarization_submission_id": "notary-submission",
        },
        "signed-artifacts/desktop-windows-signed.json": {
            "platform": "windows",
            "status": "passed",
            "source_commit": "abc123",
            "signing_run_id": "sign-windows",
            "signature_verified": True,
            "authenticode_verified": True,
            "authenticode_signer": "CN=OmniDesk",
            "authenticode_certificate_sha256": "sha256:" + "2" * 64,
        },
    }
    for rel_path, doc in native_docs.items():
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc) + "\n", encoding="utf-8")
    for rel_path, doc in signed_docs.items():
        platform = doc["platform"]
        digest = digests[platform]
        doc.update(
            {
                "signed_artifact_sha256": digest,
                "native_signed_binding_sha256": digest,
                "artifact_attestation": {
                    "attestation_id": f"attestation-{platform}",
                    "subject_sha256": digest,
                },
            }
        )
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc) + "\n", encoding="utf-8")


def _write_audit(path: Path, status: str, blocker_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema": "omnidesk-real-ga-prebinding-audit/v1", "status": status, "blocker_count": blocker_count}) + "\n",
        encoding="utf-8",
    )


def test_missing_external_evidence_is_blocked_not_passed(tmp_path, monkeypatch) -> None:
    _set_github_env(monkeypatch)
    artifacts = write_evidence(
        output_dir=tmp_path / "dist" / "evidence",
        external_evidence_root=tmp_path / "release" / "external-evidence",
        real_ga_summary="release/real-ga-evidence-summary.json",
    )

    assert artifacts["evidence"]["source_verification_status"] == "passed"
    assert artifacts["evidence"]["status"] == "blocked_missing_external_evidence"
    assert artifacts["evidence"]["real_ga_prebinding_audit_status"] == "not_supplied"
    assert artifacts["binding"]["status"] == "blocked_missing_external_evidence"
    assert artifacts["manifest"]["status"] == "blocked_missing_external_evidence"
    assert artifacts["manifest"]["customer_distribution_ga_status"] == "blocked_missing_external_evidence"
    assert artifacts["manifest"]["all_required_native_builds_present"] is False
    assert artifacts["manifest"]["all_required_signed_artifacts_present"] is False
    assert artifacts["manifest"]["all_artifact_digest_bindings_valid"] is False


def test_placeholder_files_do_not_bypass_failed_semantic_audit(tmp_path, monkeypatch) -> None:
    _set_github_env(monkeypatch)
    evidence_root = tmp_path / "release" / "external-evidence"
    audit_report = tmp_path / "prebinding-audit.json"
    _write_required_files(evidence_root)
    _write_audit(audit_report, "blocked_missing_external_evidence", 3)

    artifacts = write_evidence(
        output_dir=tmp_path / "dist" / "evidence",
        external_evidence_root=evidence_root,
        real_ga_summary="release/real-ga-evidence-summary.json",
        real_ga_audit_report=audit_report,
    )

    assert artifacts["manifest"]["all_required_native_builds_present"] is True
    assert artifacts["manifest"]["all_required_signed_artifacts_present"] is True
    assert artifacts["manifest"]["real_ga_prebinding_audit_status"] == "blocked_missing_external_evidence"
    assert artifacts["manifest"]["status"] == "blocked_missing_external_evidence"
    assert artifacts["binding"]["native_builds_bound"] is False
    assert artifacts["binding"]["signed_artifacts_bound"] is False
    assert artifacts["binding"]["all_artifact_digest_bindings_valid"] is True


def test_complete_semantically_valid_external_evidence_allows_customer_ga_pass(tmp_path, monkeypatch) -> None:
    _set_github_env(monkeypatch)
    evidence_root = tmp_path / "release" / "external-evidence"
    audit_report = tmp_path / "prebinding-audit.json"
    _write_required_files(evidence_root)
    _write_audit(audit_report, "passed", 0)

    artifacts = write_evidence(
        output_dir=tmp_path / "dist" / "evidence",
        external_evidence_root=evidence_root,
        real_ga_summary="release/real-ga-evidence-summary.json",
        real_ga_audit_report=audit_report,
    )

    assert artifacts["evidence"]["status"] == "passed"
    assert artifacts["binding"]["status"] == "passed"
    assert artifacts["manifest"]["status"] == "passed"
    assert artifacts["manifest"]["real_ga_prebinding_audit_status"] == "passed"
    assert artifacts["manifest"]["all_required_native_builds_present"] is True
    assert artifacts["manifest"]["all_required_signed_artifacts_present"] is True
    assert artifacts["manifest"]["all_artifact_digest_bindings_valid"] is True
    assert len(artifacts["binding"]["artifact_digest_bindings"]) == 4
    assert all(row["digests_match"] for row in artifacts["binding"]["artifact_digest_bindings"])
    assert (evidence_root / "control-plane" / "main-verification-evidence.json").is_file()
    assert (evidence_root / "control-plane" / "native-signed-artifact-binding.json").is_file()


def test_digest_or_platform_signature_mismatch_blocks_customer_ga(tmp_path, monkeypatch) -> None:
    _set_github_env(monkeypatch)
    evidence_root = tmp_path / "release" / "external-evidence"
    audit_report = tmp_path / "prebinding-audit.json"
    _write_required_files(evidence_root)
    _write_audit(audit_report, "passed", 0)
    signed_path = evidence_root / "signed-artifacts/android-signed-aab.json"
    signed = json.loads(signed_path.read_text(encoding="utf-8"))
    signed["signed_artifact_sha256"] = "sha256:" + "f" * 64
    signed.pop("android_signer_certificate_sha256")
    signed_path.write_text(json.dumps(signed) + "\n", encoding="utf-8")

    artifacts = write_evidence(
        output_dir=tmp_path / "dist" / "evidence",
        external_evidence_root=evidence_root,
        real_ga_summary="release/real-ga-evidence-summary.json",
        real_ga_audit_report=audit_report,
    )

    android = next(row for row in artifacts["binding"]["artifact_digest_bindings"] if row["platform"] == "android")
    assert artifacts["manifest"]["status"] == "blocked_missing_external_evidence"
    assert artifacts["manifest"]["all_artifact_digest_bindings_valid"] is False
    assert android["digests_match"] is False
    assert android["missing_signature_fields"] == ["android_signer_certificate_sha256"]


def test_main_verification_binds_every_artifact_when_platform_has_multiple_files(tmp_path, monkeypatch) -> None:
    _set_github_env(monkeypatch)
    evidence_root = tmp_path / "release" / "external-evidence"
    audit_report = tmp_path / "prebinding-audit.json"
    _write_required_files(evidence_root)
    _write_audit(audit_report, "passed", 0)
    digests = ["sha256:" + "e" * 64, "sha256:" + "f" * 64]

    native_path = evidence_root / "native-build/flutter-android-release.json"
    native = json.loads(native_path.read_text(encoding="utf-8"))
    native.pop("release_payload_artifact_sha256")
    native["artifacts"] = [
        {"path": f"app-{index}.aab", "sha256": digest, "release_payload_artifact_sha256": digest}
        for index, digest in enumerate(digests, start=1)
    ]
    native_path.write_text(json.dumps(native) + "\n", encoding="utf-8")

    signed_path = evidence_root / "signed-artifacts/android-signed-aab.json"
    signed = json.loads(signed_path.read_text(encoding="utf-8"))
    signed.pop("signed_artifact_sha256")
    signed.pop("native_signed_binding_sha256")
    signed.pop("artifact_attestation")
    signed["artifacts"] = [
        {
            "path": f"app-{index}.aab",
            "sha256": digest,
            "signed_artifact_sha256": digest,
            "native_signed_binding_sha256": digest,
            "artifact_attestation": {"attestation_id": f"att-{index}", "subject_sha256": digest},
        }
        for index, digest in enumerate(digests, start=1)
    ]
    signed_path.write_text(json.dumps(signed) + "\n", encoding="utf-8")

    artifacts = write_evidence(
        output_dir=tmp_path / "dist/evidence",
        external_evidence_root=evidence_root,
        real_ga_summary="release/real-ga-evidence-summary.json",
        real_ga_audit_report=audit_report,
    )

    android = next(row for row in artifacts["binding"]["artifact_digest_bindings"] if row["platform"] == "android")
    assert artifacts["manifest"]["status"] == "passed"
    assert android["valid"] is True
    assert len(android["artifacts"]) == 2
    assert {item["native_signed_binding_sha256"] for item in android["artifacts"]} == set(digests)
