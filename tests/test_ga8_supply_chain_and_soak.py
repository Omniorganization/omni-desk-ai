from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.check_optional_connector_coverage import OPTIONAL_CONNECTOR_MINIMUMS, PRODUCTION_CRITICAL_MINIMUMS
from scripts.check_supply_chain_standard import main as check_supply_chain_standard_main


def test_supply_chain_standard_contract_passes_current_tree():
    assert check_supply_chain_standard_main(["."]) == 0


def test_release_and_promotion_include_cosign_and_slsa_gates():
    release = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    promote = Path(".github/workflows/promote-production.yml").read_text(encoding="utf-8")
    supply = Path(".github/workflows/supply-chain.yml").read_text(encoding="utf-8")
    assert "cosign sign-blob" in release
    assert "cosign attest-blob" in release
    assert "slsa-provenance.json" in release
    assert "write_slsa_provenance.py" in release
    assert "cosign verify-blob" in promote
    assert "cosign verify-attestation" in promote
    assert "gh attestation verify" in supply
    assert "in-toto" in supply


def test_soak_workflow_and_script_contracts():
    workflow = Path(".github/workflows/soak-test.yml").read_text(encoding="utf-8")
    assert "schedule:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "scripts/soak_test.py" in workflow
    assert "soak-report.json" in workflow
    result = subprocess.run([sys.executable, "scripts/soak_test.py", "--iterations", "2", "--json"], text=True, capture_output=True, check=True)
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["iterations"] == 2
    assert report["failure_count"] == 0
    assert report["webhook_replay_reject_count"] > 0
    assert report["approval_race_reject_count"] > 0


def test_soak_test_does_not_leak_sqlite_resource_warnings():
    code = "from scripts.soak_test import run_soak; import gc; report=run_soak(iterations=2, sleep_seconds=0); gc.collect(); assert report['ok']"
    result = subprocess.run([sys.executable, "-W", "error", "-c", code], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_optional_connector_coverage_script_help_and_report(tmp_path):
    coverage = tmp_path / "coverage.json"
    coverage.write_text(json.dumps({"files": {"omnidesk_agent/channels/gmail.py": {"summary": {"covered_lines": 2, "num_statements": 4}}, "omnidesk_agent/server.py": {"summary": {"covered_lines": 9, "num_statements": 10}}}}), encoding="utf-8")
    failure_result = subprocess.run([sys.executable, "scripts/check_optional_connector_coverage.py", str(coverage), "--json"], text=True, capture_output=True)
    assert failure_result.returncode == 1
    failure_data = json.loads(failure_result.stdout)
    assert failure_data["optional_connectors"]["omnidesk_agent/channels/gmail.py"] == 50.0
    assert failure_data["production_critical"]["omnidesk_agent/server.py"] == 90.0
    assert any(item["reason"] == "missing" for item in failure_data["failures"])

    passing_files = {
        name: {"summary": {"covered_lines": int(required), "num_statements": 100}}
        for name, required in {**OPTIONAL_CONNECTOR_MINIMUMS, **PRODUCTION_CRITICAL_MINIMUMS}.items()
    }
    coverage.write_text(json.dumps({"files": passing_files}), encoding="utf-8")
    passing_result = subprocess.run([sys.executable, "scripts/check_optional_connector_coverage.py", str(coverage), "--json"], text=True, capture_output=True, check=True)
    passing_data = json.loads(passing_result.stdout)
    assert passing_data["failures"] == []


def test_running_ga_checklist_documents_real_environment_gates():
    text = Path("docs/RUNNING_GA_CHECKLIST.md").read_text(encoding="utf-8")
    for phrase in ["runtime-level GA", "Cosign", "SLSA", "Rollback", "Backup", "Soak", "Observability"]:
        assert phrase in text
