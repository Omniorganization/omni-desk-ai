from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from scripts.verify_release_artifact import main as verify_release_artifact_main


def test_release_governance_assets_exist():
    assert Path("INDUSTRIAL_SOURCE_MAIN_RESTORE.md").exists()
    assert Path("requirements.lock").exists()
    assert Path("requirements.bootstrap.lock").exists()
    assert Path("requirements.runtime.lock").exists()
    assert Path("requirements.dev.lock").exists()
    assert Path("requirements.security.lock").exists()
    assert Path("scripts/check_monorepo_layout.py").exists()
    assert Path("scripts/sign_release.py").exists()
    assert Path("scripts/package_distribution_bundle.sh").exists()
    assert Path("scripts/write_distribution_manifest.py").exists()
    assert Path("scripts/check_release_channel_policy.py").exists()
    assert Path("scripts/check_ci_evidence_contract.py").exists()
    assert Path("scripts/write_ci_evidence_manifest.py").exists()
    assert Path("scripts/check_security_workflow_policy.py").exists()
    assert Path("scripts/check_license_policy.py").exists()
    assert Path("scripts/docker_scan.sh").exists()
    assert Path("scripts/production_smoke_test.py").exists()
    assert Path("scripts/release_smoke_locked.sh").exists()
    assert Path("scripts/check_script_executability.py").exists()
    assert Path("docs/SRE_RUNBOOK.md").exists()
    assert Path(".github/workflows/promote-production.yml").exists()
    assert Path(".github/workflows/release-policy.yml").exists()
    assert Path(".github/branch-protection.required.json").exists()
    assert Path(".gitleaks.toml").exists()
    assert Path("release/license-policy.json").exists()


def test_source_main_restore_contract_blocks_package_only_main():
    contract = Path("INDUSTRIAL_SOURCE_MAIN_RESTORE.md").read_text(encoding="utf-8")
    assert "`main` as the source trunk" in contract
    assert "must not be the\n  only content on `main`" in contract
    assert "backup/package-only-1.11.8" in contract
    assert "Real GA releases must run `scripts/check_external_ga_evidence.py .` without\n  `--audit-only`" in contract


def test_release_workflow_separates_candidate_and_real_ga_evidence_gate():
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "release_channel" in workflow
    assert "RELEASE_CHANNEL" in workflow
    assert '[[ "$RELEASE_CHANNEL" == "real-ga" ]]' in workflow
    assert "check_external_ga_evidence.py . --write-report release/real-ga-evidence-audit-1.12.5.json" in workflow
    assert "check_external_ga_evidence.py . --audit-only --write-report release/real-ga-evidence-audit-1.12.5.json" in workflow


def test_release_channel_policy_script_passes_current_tree():
    result = subprocess.run([sys.executable, "scripts/check_release_channel_policy.py", "."], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "Real GA branch never uses --audit-only" in result.stdout
    assert "Release workflow rechecks channel naming and evidence status after external evidence gate" in result.stdout


def test_makefile_external_evidence_targets_keep_real_ga_fail_closed():
    makefile = Path("Makefile").read_text(encoding="utf-8")
    assert "RELEASE_CHANNEL ?= candidate" in makefile
    assert "release-external-ga-evidence:" in makefile
    assert "external-ga-evidence-gate:" in makefile
    assert "scripts/check_external_ga_evidence.py .\n" in makefile
    assert "release/real-ga-evidence-audit-1.12.5.json" in makefile


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
    assert "scripts/check_ci_evidence_contract.py" in ci
    assert "scripts/check_security_workflow_policy.py" in ci
    assert "scripts/write_ci_evidence_manifest.py" in ci
    assert "ci-evidence-${{ matrix.python-version }}" in ci
    assert "--artifact-name \"ci-evidence-${{ matrix.python-version }}\"" in ci
    writer = Path("scripts/write_ci_evidence_manifest.py").read_text(encoding="utf-8")
    assert '"job_result": "success"' in writer
    assert '"artifacts": [' in writer


def test_script_executability_contract_passes_current_tree():
    result = subprocess.run([sys.executable, "scripts/check_script_executability.py", "."], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


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
    checksums.write_text(
        "\n".join(
            [
                f"{hashlib.sha256(wheel.read_bytes()).hexdigest()}  {wheel.name}",
                f"{hashlib.sha256(sbom.read_bytes()).hexdigest()}  {sbom.name}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    artifacts = []
    for item in [checksums, sbom, wheel]:
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
    (dist / "release_signatures.json").write_text(json.dumps({"artifacts": [{"name": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}]}), encoding="utf-8")

    assert verify_release_artifact_main([str(dist), "--require-signatures"]) == 1
