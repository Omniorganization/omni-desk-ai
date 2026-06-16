from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from scripts.verify_release_artifact import main as verify_release_artifact_main


def test_release_governance_assets_exist():
    assert Path("requirements.lock").exists()
    assert Path("requirements.bootstrap.lock").exists()
    assert Path("requirements.runtime.lock").exists()
    assert Path("requirements.dev.lock").exists()
    assert Path("requirements.security.lock").exists()
    assert Path("scripts/sign_release.py").exists()
    assert Path("scripts/docker_scan.sh").exists()
    assert Path("scripts/production_smoke_test.py").exists()
    assert Path("scripts/release_smoke_locked.sh").exists()
    assert Path("scripts/check_script_executability.py").exists()
    assert Path("scripts/check_release_configuration.py").exists()
    assert Path("docs/SRE_RUNBOOK.md").exists()
    assert Path("docs/RELEASE_CONFIGURATION_PREFLIGHT.md").exists()
    assert Path(".github/workflows/promote-production.yml").exists()


def test_ci_coverage_gate_is_80_and_has_group_gate():
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "--cov-fail-under=80" in ci
    assert "scripts/check_coverage_gates.py" in ci
    assert "requirements.security.lock" in ci
    assert "requirements.bootstrap.lock" in ci
    gates = Path("scripts/check_coverage_gates.py").read_text(encoding="utf-8")
    assert "omnidesk_agent/sandbox/runner_server.py" in gates
    assert "omnidesk_agent/sandbox/remote_runner.py" in gates
    assert "omnidesk_agent/models/schema_retry.py" in gates
    assert "omnidesk_agent/self_upgrade/sandbox_runner.py" in gates
    assert "omnidesk_agent/tools/shell.py" in gates
    assert "omnidesk_agent/oauth/gmail_oauth.py" in gates

def test_version_consistency_script_passes_current_tree():
    result = subprocess.run([sys.executable, "scripts/check_version_consistency.py", "."], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    from omnidesk_agent import __version__
    assert __version__ in result.stdout


def test_ci_runs_version_consistency_check():
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "scripts/check_version_consistency.py" in ci
    assert "scripts/check_script_executability.py" in ci


def test_script_executability_contract_passes_current_tree():
    result = subprocess.run([sys.executable, "scripts/check_script_executability.py", "."], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_drill_workflows_install_locked_python_dependencies() -> None:
    for workflow_name in ["backup-restore-drill.yml", "production-closure-drill.yml"]:
        workflow = Path(".github/workflows", workflow_name).read_text(encoding="utf-8")
        assert "python -m pip install --require-hashes -r requirements.dev.lock" in workflow
        assert "python -m pip install -e . --no-deps --no-build-isolation" in workflow


def test_release_and_downstream_workflows_fail_fast_on_missing_github_config() -> None:
    release = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "release-config-preflight" in release
    assert "python3 scripts/check_release_configuration.py --scope release" in release
    assert "OMNI_ANDROID_KEYSTORE_BASE64: ${{ secrets.OMNI_ANDROID_KEYSTORE_BASE64 }}" in release
    assert "OMNIDESK_SANDBOX_RUNNER_DIGEST: ${{ vars.OMNIDESK_SANDBOX_RUNNER_DIGEST }}" in release
    assert release.count("needs: release-config-preflight") >= 4

    downstream = {
        "deploy-staging.yml": "--scope staging",
        "promote-production.yml": "--scope production",
        "rollback-drill.yml": "--scope rollback",
    }
    for workflow_name, scope_arg in downstream.items():
        workflow = Path(".github/workflows", workflow_name).read_text(encoding="utf-8")
        assert "python3 scripts/check_release_configuration.py" in workflow
        assert scope_arg in workflow
        assert "OMNIDESK_RELEASE_SIGNING_KEY: ${{ secrets.OMNIDESK_RELEASE_SIGNING_KEY }}" in workflow


def test_observability_and_full_compose_assets_exist():
    assert Path("deploy/observability/prometheus-rules.yml").exists()
    assert Path("deploy/observability/grafana-dashboard.json").exists()
    assert Path("scripts/check_observability_contract.py").exists()
    result = subprocess.run([sys.executable, "scripts/check_observability_contract.py", "."], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    full_compose = Path("deploy/docker/docker-compose.full.yml").read_text(encoding="utf-8")
    assert "sandbox-runner" in full_compose
    assert "omnidesk-private" in full_compose

def test_verify_release_artifact_accepts_build_release_signature_shape(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "pkg-0.0.1-py3-none-any.whl"
    wheel.write_bytes(b"not-a-real-wheel")
    sbom = dist / "sbom.json"
    sbom.write_text("{}", encoding="utf-8")
    checksums = dist / "checksums.txt"
    checksum_manifest = "\n".join(
        [
            f"{hashlib.sha256(wheel.read_bytes()).hexdigest()}  {wheel.name}",
            f"{hashlib.sha256(sbom.read_bytes()).hexdigest()}  {sbom.name}",
        ]
    ) + "\n"
    checksums.write_text(checksum_manifest, encoding="utf-8")
    standard_checksums = dist / "SHA256SUMS.txt"
    standard_checksums.write_text(checksum_manifest, encoding="utf-8")
    artifacts = []
    for item in [checksums, standard_checksums, sbom, wheel]:
        artifacts.append({"name": item.name, "sha256": hashlib.sha256(item.read_bytes()).hexdigest()})
        (dist / f"{item.name}.sig").write_bytes(b"test-signature")
    (dist / "release_signatures.json").write_text(json.dumps({"artifacts": artifacts}), encoding="utf-8")

    assert verify_release_artifact_main([str(dist), "--require-signatures"]) == 0


def test_verify_release_artifact_rejects_signature_set_mismatch(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "pkg-0.0.1-py3-none-any.whl"
    wheel.write_bytes(b"not-a-real-wheel")
    sbom = dist / "sbom.json"
    sbom.write_text("{}", encoding="utf-8")
    metadata = dist / "release_metadata.json"
    metadata.write_text(json.dumps({"version": "0.0.1", "artifact": {"sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}}), encoding="utf-8")
    checksum_lines = []
    for item in sorted([wheel, sbom, metadata]):
        checksum_lines.append(f"{hashlib.sha256(item.read_bytes()).hexdigest()}  {item.name}")
    checksums = dist / "checksums.txt"
    checksums.write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    (dist / "SHA256SUMS.txt").write_text(checksums.read_text(encoding="utf-8"), encoding="utf-8")
    (dist / "release_signatures.json").write_text(json.dumps({"artifacts": [{"name": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}]}), encoding="utf-8")

    assert verify_release_artifact_main([str(dist), "--require-signatures"]) == 1
