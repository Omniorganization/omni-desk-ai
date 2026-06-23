from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.check_ci_evidence_contract import main as check_ci_evidence_contract_main
from scripts.check_license_policy import main as check_license_policy_main
from scripts.check_production_install_policy import main as check_production_install_policy_main
from scripts.check_security_workflow_policy import main as check_security_workflow_policy_main
from scripts.write_ci_evidence_manifest import main as write_ci_evidence_manifest_main


VERSION = "1.12.5+root-monorepo-production-ga-candidate"
SLUG = "Omni-desk-AI-1.12.5-root-monorepo-production-ga-candidate"


def test_write_ci_evidence_manifest_binds_commit_run_logs_and_coverage(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nversion = "1.12.5+root-monorepo-production-ga-candidate"\n', encoding="utf-8")
    coverage_json = tmp_path / "coverage.json"
    coverage_json.write_text(
        json.dumps({"totals": {"covered_lines": 8, "num_statements": 10, "percent_covered": 80.0}}),
        encoding="utf-8",
    )
    coverage_xml = tmp_path / "coverage.xml"
    coverage_xml.write_text("<coverage line-rate=\"0.8\" />\n", encoding="utf-8")
    ruff_log = tmp_path / "ruff.txt"
    ruff_log.write_text("All checks passed!\n", encoding="utf-8")
    pytest_log = tmp_path / "pytest.txt"
    pytest_log.write_text("======================== 3 passed in 0.12s ========================\n", encoding="utf-8")
    output = tmp_path / "reports" / "ci-evidence.json"

    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_WORKFLOW", "CI")
    monkeypatch.setenv("GITHUB_JOB", "test")

    assert (
        write_ci_evidence_manifest_main(
            [
                "--root",
                str(root),
                "--output",
                str(output),
                "--python-version",
                "3.11",
                "--coverage-json",
                str(coverage_json),
                "--coverage-xml",
                str(coverage_xml),
                "--log",
                f"ruff={ruff_log}",
                "--log",
                f"pytest={pytest_log}",
            ]
        )
        == 0
    )
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "omnidesk-ci-evidence/v1"
    assert manifest["project_version"] == VERSION
    assert manifest["source_commit"] == "a" * 40
    assert manifest["github"]["run_url"] == "https://github.com/owner/repo/actions/runs/12345"
    assert manifest["matrix"]["python_version"] == "3.11"
    assert manifest["coverage"]["json"]["percent_covered"] == 80.0
    assert manifest["logs"][1]["summary"] == "3 passed in 0.12s"


def test_ci_and_security_workflow_policy_contracts_pass_current_tree() -> None:
    assert check_ci_evidence_contract_main(["."]) == 0
    assert check_security_workflow_policy_main(["."]) == 0
    assert check_production_install_policy_main(["."]) == 0


def test_security_workflow_dependency_review_and_all_python_locks_are_blocking() -> None:
    workflow = Path(".github/workflows/security.yml").read_text(encoding="utf-8")
    dependency_review = workflow[workflow.index("actions/dependency-review-action@"):workflow.index("actions/dependency-review-action@") + 300]
    assert "continue-on-error: true" not in dependency_review
    for lockfile in [
        "requirements.lock",
        "requirements.runtime.lock",
        "requirements.bootstrap.lock",
        "requirements.dev.lock",
        "requirements.security.lock",
        "requirements.enterprise.lock",
    ]:
        assert f"python scripts/check_lock_hashes.py {lockfile}" in workflow
        assert f"pip-audit --disable-pip -r {lockfile}" in workflow


def test_license_policy_skips_lockfile_packages_that_do_not_match_current_marker(tmp_path: Path) -> None:
    lockfile = tmp_path / "requirements.lock"
    lockfile.write_text("definitely-not-installed==1.0.0 ; python_full_version < '0'\n", encoding="utf-8")
    policy = tmp_path / "license-policy.json"
    policy.write_text(
        json.dumps(
            {
                "allow_unknown_license": False,
                "allowed_packages": [],
                "denied_license_terms": ["GPL"],
                "schema_version": "omnidesk-license-policy/v1",
            }
        ),
        encoding="utf-8",
    )
    assert check_license_policy_main(["--lockfile", str(lockfile), "--policy", str(policy)]) == 0


def test_release_channel_policy_accepts_candidate_and_rejects_real_ga_candidate_names() -> None:
    candidate = subprocess.run(
        [
            sys.executable,
            "scripts/check_release_channel_policy.py",
            ".",
            "--release-channel",
            "candidate",
            "--package-version",
            VERSION,
            "--package-slug",
            SLUG,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert candidate.returncode == 0, candidate.stderr
    assert "Candidate package version carries candidate/source-gated status" in candidate.stdout

    real_ga = subprocess.run(
        [
            sys.executable,
            "scripts/check_release_channel_policy.py",
            ".",
            "--release-channel",
            "real-ga",
            "--package-version",
            VERSION,
            "--package-slug",
            SLUG,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert real_ga.returncode == 1
    assert "Real GA package version does not carry candidate/source-gated status" in real_ga.stderr
    assert "Real GA evidence blocker_count is zero" in real_ga.stderr
