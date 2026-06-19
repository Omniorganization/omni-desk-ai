from __future__ import annotations

import json
from pathlib import Path

from scripts.import_tri_app_live_smoke_evidence import main


def _write_report(
    path: Path,
    *,
    passed: bool = True,
    trace_id: str = "trace-001",
    started_at: str = "2026-06-17T00:00:00Z",
    finished_at: str = "2026-06-17T00:00:03Z",
    latency_ms: int = 3000,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "tri-app-live-smoke/v1",
        "status": "passed",
        "scenario_id": "approval-roundtrip-001",
        "org_id": "org_demo_001",
        "trace_id": trace_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "latency_ms": latency_ms,
        "steps": {
            "desktop_action_proposed": True,
            "backend_approval_created": True,
            "mobile_push_received": True,
            "mobile_approval_decision_submitted": True,
            "desktop_action_resumed": True,
            "audit_event_written": True,
            "web_admin_audit_visible": passed,
        },
    }), encoding="utf-8")


def test_import_tri_app_live_smoke_evidence_accepts_and_copies(tmp_path, capsys) -> None:
    report = tmp_path / "tri-app-live-smoke.json"
    dest = tmp_path / "dest"
    audit = tmp_path / "audit.json"
    _write_report(report)

    assert main([
        "--report", str(report),
        "--copy",
        "--dest-dir", str(dest),
        "--expected-org-id", "org_demo_001",
        "--expected-scenario-id", "approval-roundtrip-001",
        "--write-report", str(audit),
    ]) == 0
    assert (dest / "tri-app-live-smoke.json").exists()
    assert '"status": "passed"' in capsys.readouterr().out


def test_import_tri_app_live_smoke_evidence_rejects_failed_step(tmp_path, capsys) -> None:
    report = tmp_path / "tri-app-live-smoke.json"
    _write_report(report, passed=False)

    assert main(["--report", str(report), "--write-report", str(tmp_path / "audit.json")]) == 1
    assert "steps.web_admin_audit_visible must be true" in capsys.readouterr().out


def test_import_tri_app_live_smoke_evidence_rejects_org_mismatch(tmp_path, capsys) -> None:
    report = tmp_path / "tri-app-live-smoke.json"
    _write_report(report)

    assert main(["--report", str(report), "--expected-org-id", "wrong", "--write-report", str(tmp_path / "audit.json")]) == 1
    assert "org_id mismatch" in capsys.readouterr().out


def test_import_tri_app_live_smoke_evidence_rejects_raw_sensitive_field(tmp_path, capsys) -> None:
    report = tmp_path / "tri-app-live-smoke.json"
    _write_report(report)
    doc = json.loads(report.read_text(encoding="utf-8"))
    doc["admin_token"] = "raw-admin-token-value"
    report.write_text(json.dumps(doc), encoding="utf-8")

    assert main(["--report", str(report), "--write-report", str(tmp_path / "audit.json")]) == 1
    assert "sensitive raw field is not allowed in tri-app smoke evidence: admin_token" in capsys.readouterr().out


def test_import_tri_app_live_smoke_evidence_rejects_inverted_time(tmp_path, capsys) -> None:
    report = tmp_path / "tri-app-live-smoke.json"
    _write_report(report, started_at="2026-06-17T00:00:03Z", finished_at="2026-06-17T00:00:00Z")

    assert main(["--report", str(report), "--write-report", str(tmp_path / "audit.json")]) == 1
    assert "finished_at must be after started_at" in capsys.readouterr().out


def test_import_tri_app_live_smoke_evidence_rejects_latency_mismatch(tmp_path, capsys) -> None:
    report = tmp_path / "tri-app-live-smoke.json"
    _write_report(report, latency_ms=99)

    assert main(["--report", str(report), "--write-report", str(tmp_path / "audit.json")]) == 1
    assert "latency_ms must be within 10%" in capsys.readouterr().out


def test_import_tri_app_live_smoke_evidence_rejects_placeholder_trace(tmp_path, capsys) -> None:
    report = tmp_path / "tri-app-live-smoke.json"
    _write_report(report, trace_id="placeholder")

    assert main(["--report", str(report), "--write-report", str(tmp_path / "audit.json")]) == 1
    assert "trace_id must be a non-placeholder safe id" in capsys.readouterr().out
