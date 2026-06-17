from __future__ import annotations

import json
import shutil
from pathlib import Path

from scripts import check_external_ga_evidence, external_ga_evidence_doctor


def test_external_ga_evidence_doctor_builds_missing_plan(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copy("pyproject.toml", root / "pyproject.toml")

    plan = external_ga_evidence_doctor.build_plan(root, root / "release" / "external-evidence")

    expected_files = sum(len(spec["files"]) for spec in check_external_ga_evidence.REQUIRED_EVIDENCE.values())
    assert plan["status"] == "blocked_missing_external_evidence"
    assert plan["missing_count"] == expected_files
    assert any(item["category"] == "postgres_soak" for item in plan["missing"])
    assert all("runbook_hint" in item for item in plan["missing"])


def test_external_ga_evidence_doctor_writes_blocked_templates(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copy("pyproject.toml", root / "pyproject.toml")

    written = external_ga_evidence_doctor.write_templates(root, root / "dist" / "templates")

    assert "dist/templates/drills/postgres-multi-instance-soak.json" in written
    template = json.loads((root / "dist" / "templates" / "drills" / "postgres-multi-instance-soak.json").read_text())
    assert template["status"] == "blocked_pending_real_run"
    assert template["gateway_count"] == 0
    assert template["policy"].startswith("Fill only with evidence produced by real external systems")


def test_external_ga_evidence_doctor_cli_writes_plan_and_templates(tmp_path: Path, capsys) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copy("pyproject.toml", root / "pyproject.toml")

    assert external_ga_evidence_doctor.main([str(root)]) == 0

    captured = capsys.readouterr()
    assert "blocked_missing_external_evidence" in captured.out
    assert (root / "dist" / "external-ga-evidence-plan.json").exists()
    assert (root / "dist" / "external-ga-evidence-templates" / "push" / "fcm-live-delivery.json").exists()
