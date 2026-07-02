from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

from scripts.assemble_external_ga_evidence_bundle import main as assemble_main
from scripts.check_release_configuration import main as config_main
from scripts.import_external_ga_evidence import main
from scripts.import_ios_real_device_evidence import VERSION


def _artifact(raw: Path, rel: str, content: bytes = b"artifact") -> str:
    path = raw / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _write_doc(raw: Path, rel: str, doc: dict) -> None:
    path = raw / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _base_doc(**extra):
    doc = {
        "status": "passed",
        "produced_at": "2026-07-02T00:00:00Z",
        "producer": "ci-run-123",
        "environment": "staging",
    }
    doc.update(extra)
    return doc


def _artifact_doc(raw: Path, rel: str, artifact_rel: str, **extra):
    digest = _artifact(raw, artifact_rel, rel.encode())
    return _base_doc(
        version=VERSION,
        artifacts=[{"path": artifact_rel, "sha256": digest}],
        **extra,
    )


def _write_complete_raw_evidence(raw: Path) -> None:
    native = {
        "native-build/flutter-android-release.json": (
            "artifacts/native/android.aab",
            "android",
            "flutter build appbundle --release",
        ),
        "native-build/flutter-ios-release.json": (
            "artifacts/native/ios.ipa",
            "ios",
            "flutter build ipa --release",
        ),
        "native-build/tauri-desktop-release.json": (
            "artifacts/native/desktop.dmg",
            "macos",
            "cargo tauri build",
        ),
        "native-build/rust-cargo-check-locked.json": (
            "artifacts/native/cargo-check.log",
            "rust",
            "cargo check --locked",
        ),
    }
    for rel, (artifact_rel, platform, command) in native.items():
        _write_doc(
            raw,
            rel,
            _artifact_doc(
                raw, rel, artifact_rel, platform=platform, command=command, exit_code=0
            ),
        )

    signed = {
        "signed-artifacts/android-signed-aab.json": (
            "artifacts/signed/android.aab",
            "android",
            False,
        ),
        "signed-artifacts/ios-signed-ipa.json": (
            "artifacts/signed/ios.ipa",
            "ios",
            False,
        ),
        "signed-artifacts/desktop-macos-notarized.json": (
            "artifacts/signed/desktop.dmg",
            "macos",
            True,
        ),
        "signed-artifacts/desktop-windows-signed.json": (
            "artifacts/signed/desktop.exe",
            "windows",
            False,
        ),
    }
    for rel, (artifact_rel, platform, notarized) in signed.items():
        _write_doc(
            raw,
            rel,
            _artifact_doc(
                raw,
                rel,
                artifact_rel,
                platform=platform,
                signature_verified=True,
                notarization_verified=notarized,
            ),
        )

    _write_doc(
        raw,
        "control-plane/github-branch-protection-live.json",
        {
            "schema": "omnidesk-live-branch-protection/v2",
            "status": "passed",
            "repository": "owner/repo",
            "branch": "main",
            "failures": [],
        },
    )
    _write_doc(
        raw,
        "model/model-live-smoke.json",
        _base_doc(
            schema="omnidesk-model-live-smoke/v1",
            backend_base_url="https://staging.omnidesk.internal",
            scenario_id="model-smoke-001",
            model_request_id="model-request-001",
            trace_id="trace-real-001",
            audit_event_id="audit-event-001",
            cost_ledger_entry_id="ledger-entry-001",
            response_non_empty=True,
            audit_logged=True,
            cost_ledger_recorded=True,
            budget_enforced=True,
            approval_required_on_budget_exceeded=True,
            p95_latency_ms=1200,
            error_rate=0,
        ),
    )
    _write_doc(
        raw,
        "integrations/bigseller-live-smoke.json",
        _base_doc(
            schema="omnidesk-bigseller-live-smoke/v1",
            store_id="store-001",
            trace_id="trace-bigseller-001",
            audit_event_id="audit-bigseller-001",
            auth_success=True,
            order_list_success=True,
            inventory_list_success=True,
            webhook_signature_verified=True,
            webhook_replay_guard_verified=True,
            secret_leakage_checked=True,
            no_secret_leakage=True,
            p95_latency_ms=900,
            error_rate=0,
        ),
    )
    _write_doc(
        raw,
        "push/apns-live-delivery.json",
        _base_doc(
            provider="apns",
            delivery_success=True,
            delivery_receipt_id="apns-receipt-001",
        ),
    )
    _write_doc(
        raw,
        "push/fcm-live-delivery.json",
        _base_doc(
            provider="fcm", delivery_success=True, delivery_receipt_id="fcm-receipt-001"
        ),
    )
    _write_doc(
        raw,
        "drills/postgres-multi-instance-soak.json",
        _base_doc(
            schema="omnidesk-postgres-soak/v1",
            gateway_count=3,
            worker_count=2,
            duration_minutes=60,
            critical_failures=0,
        ),
    )
    _write_doc(
        raw,
        "drills/rollback-drill.json",
        _base_doc(
            schema="omnidesk-rollback-drill/v1",
            failed_rollout=True,
            rollback_action="kubectl rollout undo deployment/omnidesk",
            slo_recovered=True,
            recovery_verified=True,
        ),
    )
    _write_doc(
        raw,
        "drills/backup-restore-drill.json",
        _base_doc(
            schema="omnidesk-backup-restore-drill/v1",
            backup_verified=True,
            restore_verified=True,
            rpo_seconds=30,
            rto_seconds=120,
        ),
    )
    _write_doc(
        raw,
        "drills/self-healing-failure-injection.json",
        _base_doc(
            schema="omnidesk-self-healing-failure-injection/v1",
            failure_injections=["worker-kill-signal"],
            containment_action="worker restarted",
            recovery_verified=True,
            post_recovery_health="passed",
        ),
    )


def test_import_external_ga_evidence_accepts_complete_bundle(tmp_path, capsys) -> None:
    raw = tmp_path / "raw"
    dest = tmp_path / "dest"
    report = tmp_path / "report.json"
    _write_complete_raw_evidence(raw)

    assert (
        main(
            [
                "--raw-dir",
                str(raw),
                "--dest-dir",
                str(dest),
                "--copy",
                "--expected-version",
                VERSION,
                "--write-report",
                str(report),
            ]
        )
        == 0
    )

    assert (dest / "drills" / "rollback-drill.json").exists()
    assert (dest / "artifacts" / "signed" / "android.aab").exists()
    assert '"status": "passed"' in capsys.readouterr().out


def test_assemble_external_ga_evidence_bundle_accepts_provider_artifact(
    tmp_path, capsys
) -> None:
    raw = tmp_path / "raw"
    provider = tmp_path / "provider-artifact" / "release" / "external-evidence"
    dest = tmp_path / "assembled"
    report = tmp_path / "assembly.json"
    _write_complete_raw_evidence(raw)
    shutil.copytree(raw, provider)

    assert (
        assemble_main(
            [
                "--root",
                ".",
                "--source-dir",
                str(provider.parent.parent),
                "--dest-dir",
                str(dest),
                "--expected-version",
                VERSION,
                "--write-report",
                str(report),
            ]
        )
        == 0
    )

    assert (dest / "drills" / "rollback-drill.json").exists()
    assert (dest / "artifacts" / "signed" / "android.aab").exists()
    assert '"status": "passed"' in capsys.readouterr().out


def test_assemble_external_ga_evidence_bundle_rejects_conflicting_provider_files(
    tmp_path, capsys
) -> None:
    raw = tmp_path / "raw"
    provider_a = tmp_path / "provider-a" / "release" / "external-evidence"
    provider_b = tmp_path / "provider-b" / "release" / "external-evidence"
    dest = tmp_path / "assembled"
    _write_complete_raw_evidence(raw)
    shutil.copytree(raw, provider_a)
    shutil.copytree(raw, provider_b)
    (provider_b / "model" / "model-live-smoke.json").write_text(
        json.dumps(
            _base_doc(
                schema="omnidesk-model-live-smoke/v1",
                backend_base_url="https://staging.omnidesk.internal",
                scenario_id="model-smoke-conflict",
                model_request_id="model-request-conflict",
                trace_id="trace-real-conflict",
                audit_event_id="audit-event-conflict",
                cost_ledger_entry_id="ledger-entry-conflict",
                response_non_empty=True,
                audit_logged=True,
                cost_ledger_recorded=True,
                budget_enforced=True,
                approval_required_on_budget_exceeded=True,
                p95_latency_ms=1300,
                error_rate=0,
            )
        ),
        encoding="utf-8",
    )

    assert (
        assemble_main(
            [
                "--root",
                ".",
                "--source-dir",
                str(provider_a.parent.parent),
                "--source-dir",
                str(provider_b.parent.parent),
                "--dest-dir",
                str(dest),
                "--expected-version",
                VERSION,
            ]
        )
        == 1
    )

    assert "conflicting evidence file" in capsys.readouterr().out


def test_import_external_ga_evidence_rejects_incomplete_bundle_without_copy(
    tmp_path, capsys
) -> None:
    raw = tmp_path / "raw"
    dest = tmp_path / "dest"
    _write_complete_raw_evidence(raw)
    (raw / "push" / "fcm-live-delivery.json").unlink()

    assert (
        main(
            [
                "--raw-dir",
                str(raw),
                "--dest-dir",
                str(dest),
                "--copy",
                "--expected-version",
                VERSION,
                "--write-report",
                str(tmp_path / "report.json"),
            ]
        )
        == 1
    )

    assert not (dest / "drills" / "rollback-drill.json").exists()
    assert (
        "missing evidence file: push/fcm-live-delivery.json" in capsys.readouterr().out
    )


def test_import_external_ga_evidence_rejects_version_mismatch(tmp_path, capsys) -> None:
    raw = tmp_path / "raw"
    _write_complete_raw_evidence(raw)

    assert (
        main(
            [
                "--raw-dir",
                str(raw),
                "--expected-version",
                "9.9.9+wrong",
                "--write-report",
                str(tmp_path / "report.json"),
            ]
        )
        == 1
    )

    assert "version must be 9.9.9+wrong" in capsys.readouterr().out


def test_external_ga_evidence_preflight_accepts_complete_bundle(
    tmp_path, monkeypatch, capsys
) -> None:
    raw = tmp_path / "raw"
    _write_complete_raw_evidence(raw)
    monkeypatch.setenv("EXTERNAL_GA_EVIDENCE_RAW_DIR", str(raw))
    monkeypatch.setenv("EXTERNAL_GA_EVIDENCE_EXPECTED_VERSION", VERSION)

    assert config_main(["--scope", "external-ga-evidence", "--format", "json"]) == 0

    assert '"ok": true' in capsys.readouterr().out
