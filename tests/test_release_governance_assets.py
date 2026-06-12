from __future__ import annotations

from pathlib import Path


def test_release_governance_assets_exist():
    assert Path("requirements.lock").exists()
    assert Path("requirements.runtime.lock").exists()
    assert Path("requirements.dev.lock").exists()
    assert Path("requirements.security.lock").exists()
    assert Path("scripts/sign_release.py").exists()
    assert Path("scripts/docker_scan.sh").exists()
    assert Path("scripts/production_smoke_test.py").exists()
    assert Path("scripts/release_smoke_locked.sh").exists()
    assert Path("docs/SRE_RUNBOOK.md").exists()


def test_ci_coverage_gate_is_80_and_has_group_gate():
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "--cov-fail-under=80" in ci
    assert "scripts/check_coverage_gates.py" in ci
    assert "requirements.security.lock" in ci
    gates = Path("scripts/check_coverage_gates.py").read_text(encoding="utf-8")
    assert "omnidesk_agent/sandbox/runner_server.py" in gates
    assert "omnidesk_agent/sandbox/remote_runner.py" in gates
    assert "omnidesk_agent/models/schema_retry.py" in gates
    assert "omnidesk_agent/self_upgrade/sandbox_runner.py" in gates
    assert "omnidesk_agent/tools/shell.py" in gates
    assert "omnidesk_agent/oauth/gmail_oauth.py" in gates
