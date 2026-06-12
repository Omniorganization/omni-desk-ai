from __future__ import annotations

from pathlib import Path


def test_release_governance_assets_exist():
    assert Path("requirements.lock").exists()
    assert Path("scripts/sign_release.py").exists()
    assert Path("scripts/docker_scan.sh").exists()
    assert Path("scripts/production_smoke_test.py").exists()
    assert Path("docs/SRE_RUNBOOK.md").exists()


def test_ci_coverage_gate_is_75_and_has_group_gate():
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "--cov-fail-under=75" in ci
    assert "scripts/check_coverage_gates.py" in ci
