
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.import_ios_real_device_evidence import VERSION, main


def _artifact(path: Path, content: bytes = b"artifact") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _write_doc(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _write_complete_raw_evidence(raw: Path) -> None:
    ipa_hash = _artifact(raw / "artifacts" / "OmniDesk.ipa")
    receipt_hash = _artifact(raw / "artifacts" / "ios" / "apns-delivery-receipt.json", b'{"receipt":"ok"}')
    _write_doc(raw / "native-build/flutter-ios-release.json", {
        "status": "ok",
        "produced_at": "2026-06-17T00:00:00Z",
        "producer": "ci",
        "platform": "ios",
        "version": VERSION,
        "command": "flutter build ipa --release",
        "exit_code": 0,
        "artifacts": [{"kind": "ios_unsigned_or_exported_ipa", "path": "artifacts/OmniDesk.ipa", "sha256": ipa_hash}],
        "smoke_cases": {"archive_created": True, "ipa_exported": True, "codesign_metadata_present": True},
    })
    _write_doc(raw / "signed-artifacts/ios-signed-ipa.json", {
        "status": "verified",
        "produced_at": "2026-06-17T00:00:00Z",
        "producer": "device-lab",
        "platform": "ios",
        "version": VERSION,
        "signature_verified": True,
        "source_native_artifact_sha256": ipa_hash,
        "artifacts": [{"kind": "ios_signed_ipa", "path": "artifacts/OmniDesk.ipa", "sha256": ipa_hash}],
        "smoke_cases": {
            "install_to_real_device": True,
            "launch_success": True,
            "gateway_connect": True,
            "device_enrollment": True,
            "mobile_chat": True,
            "approval_decision": True,
            "biometric_or_pin_confirm": True,
        },
    })
    _write_doc(raw / "push/apns-live-delivery.json", {
        "status": "passed",
        "produced_at": "2026-06-17T00:00:00Z",
        "producer": "device-lab",
        "platform": "ios",
        "version": VERSION,
        "provider": "apns",
        "delivery_success": True,
        "delivery_receipt_id": "apns-receipt-001",
        "artifacts": [{"kind": "apns_provider_receipt", "path": "artifacts/ios/apns-delivery-receipt.json", "sha256": receipt_hash}],
        "smoke_cases": {
            "permission_requested": True,
            "token_registered_with_gateway": True,
            "provider_accepted_message": True,
            "device_received_notification": True,
        },
    })


def test_import_ios_real_device_evidence_accepts_and_copies(tmp_path, capsys) -> None:
    raw = tmp_path / "raw"
    dest = tmp_path / "dest"
    report = tmp_path / "report.json"
    _write_complete_raw_evidence(raw)

    assert main(["--raw-dir", str(raw), "--dest-dir", str(dest), "--copy", "--write-report", str(report)]) == 0
    captured = capsys.readouterr()
    assert '"status": "passed"' in captured.out
    assert (dest / "native-build/flutter-ios-release.json").exists()
    assert '"status": "passed"' in report.read_text(encoding="utf-8")


def test_import_ios_real_device_evidence_rejects_placeholders(tmp_path, capsys) -> None:
    raw = tmp_path / "raw"
    _write_complete_raw_evidence(raw)
    doc = json.loads((raw / "signed-artifacts/ios-signed-ipa.json").read_text(encoding="utf-8"))
    doc["producer"] = "mock-device-lab"
    _write_doc(raw / "signed-artifacts/ios-signed-ipa.json", doc)

    assert main(["--raw-dir", str(raw), "--write-report", str(tmp_path / "report.json")]) == 1
    captured = capsys.readouterr()
    assert "placeholder/mock/example values are not accepted" in captured.out


def test_import_ios_real_device_evidence_rejects_missing_artifact_file(tmp_path, capsys) -> None:
    raw = tmp_path / "raw"
    _write_complete_raw_evidence(raw)
    (raw / "artifacts" / "OmniDesk.ipa").unlink()

    assert main(["--raw-dir", str(raw), "--write-report", str(tmp_path / "report.json")]) == 1
    captured = capsys.readouterr()
    assert "artifact file is missing" in captured.out


def test_import_ios_real_device_evidence_rejects_artifact_path_escape(tmp_path, capsys) -> None:
    raw = tmp_path / "raw"
    _write_complete_raw_evidence(raw)
    doc_path = raw / "native-build/flutter-ios-release.json"
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    doc["artifacts"] = [{"path": "../outside.ipa", "sha256": "a" * 64}]
    doc_path.write_text(json.dumps(doc), encoding="utf-8")

    assert main(["--raw-dir", str(raw), "--write-report", str(tmp_path / "report.json")]) == 1
    captured = capsys.readouterr()
    assert "canonical without . or .. segments" in captured.out


def test_import_ios_real_device_evidence_rejects_apns_without_artifact(tmp_path, capsys) -> None:
    raw = tmp_path / "raw"
    _write_complete_raw_evidence(raw)
    doc_path = raw / "push/apns-live-delivery.json"
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    doc.pop("artifacts")
    doc_path.write_text(json.dumps(doc), encoding="utf-8")

    assert main(["--raw-dir", str(raw), "--write-report", str(tmp_path / "report.json")]) == 1
    captured = capsys.readouterr()
    assert "missing required field: artifacts" in captured.out or "artifacts must contain" in captured.out


def test_import_ios_real_device_evidence_rejects_raw_sensitive_field(tmp_path, capsys) -> None:
    raw = tmp_path / "raw"
    _write_complete_raw_evidence(raw)
    doc_path = raw / "push/apns-live-delivery.json"
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    doc["push_token"] = "raw-provider-token-value"
    doc_path.write_text(json.dumps(doc), encoding="utf-8")

    assert main(["--raw-dir", str(raw), "--write-report", str(tmp_path / "report.json")]) == 1
    assert "sensitive raw field is not allowed in evidence: push_token" in capsys.readouterr().out


def test_import_ios_real_device_evidence_rejects_native_build_exit_code_failure(tmp_path, capsys) -> None:
    raw = tmp_path / "raw"
    _write_complete_raw_evidence(raw)
    doc_path = raw / "native-build/flutter-ios-release.json"
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    doc["exit_code"] = 1
    doc_path.write_text(json.dumps(doc), encoding="utf-8")

    assert main(["--raw-dir", str(raw), "--write-report", str(tmp_path / "report.json")]) == 1
    assert "exit_code must be 0" in capsys.readouterr().out


def test_import_ios_real_device_evidence_rejects_expected_version_mismatch(tmp_path, capsys) -> None:
    raw = tmp_path / "raw"
    _write_complete_raw_evidence(raw)

    assert main([
        "--raw-dir",
        str(raw),
        "--expected-version",
        "1.11.6+real-ga-evidence-closure",
        "--write-report",
        str(tmp_path / "report.json"),
    ]) == 1
    assert "version must be 1.11.6+real-ga-evidence-closure" in capsys.readouterr().out


def test_import_ios_real_device_evidence_rejects_apns_ipa_artifact_kind(tmp_path, capsys) -> None:
    raw = tmp_path / "raw"
    _write_complete_raw_evidence(raw)
    ipa_hash = hashlib.sha256(b"artifact").hexdigest()
    doc_path = raw / "push/apns-live-delivery.json"
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    doc["artifacts"] = [{"kind": "apns_provider_receipt", "path": "artifacts/OmniDesk.ipa", "sha256": ipa_hash}]
    doc_path.write_text(json.dumps(doc), encoding="utf-8")

    assert main(["--raw-dir", str(raw), "--write-report", str(tmp_path / "report.json")]) == 1
    assert "APNS delivery evidence artifact must not be an IPA" in capsys.readouterr().out


def test_import_ios_real_device_evidence_rejects_apns_artifact_without_kind(tmp_path, capsys) -> None:
    raw = tmp_path / "raw"
    _write_complete_raw_evidence(raw)
    doc_path = raw / "push/apns-live-delivery.json"
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    doc["artifacts"][0].pop("kind")
    doc_path.write_text(json.dumps(doc), encoding="utf-8")

    assert main(["--raw-dir", str(raw), "--write-report", str(tmp_path / "report.json")]) == 1
    assert "APNS artifact kind must be one of" in capsys.readouterr().out
