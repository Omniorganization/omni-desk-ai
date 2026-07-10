from __future__ import annotations

import json
from pathlib import Path

from scripts.write_main_verification_evidence import NATIVE_REQUIRED, SIGNED_REQUIRED, write_evidence


def _set_github_env(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_WORKFLOW", "Main Verification")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/omnidesk")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")


def _write_required_files(root: Path) -> None:
    for rel_path in (*NATIVE_REQUIRED, *SIGNED_REQUIRED):
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"status":"passed"}\n', encoding="utf-8")


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
    assert (evidence_root / "control-plane" / "main-verification-evidence.json").is_file()
    assert (evidence_root / "control-plane" / "native-signed-artifact-binding.json").is_file()
