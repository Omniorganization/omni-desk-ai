from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMIT = "a" * 40
BASE_DIGEST = "sha256:" + "b" * 64


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_web_supply_chain_binding_writes_spdx_slsa_and_manifest(tmp_path: Path) -> None:
    artifact = tmp_path / "web-image.tar.gz"
    artifact.write_bytes(b"bounded web image bytes")
    output = tmp_path / "evidence"
    result = run(
        "scripts/write_web_supply_chain_binding.py",
        "--artifact",
        str(artifact),
        "--image-ref",
        f"omnidesk-web-admin:{COMMIT}",
        "--image-id",
        "sha256:" + "c" * 64,
        "--node-base-name",
        "node:22-bookworm-slim",
        "--node-base-image",
        f"node@{BASE_DIGEST}",
        "--node-base-digest",
        BASE_DIGEST,
        "--source-commit",
        COMMIT,
        "--repository",
        "Omniorganization/omni-desk-ai",
        "--workflow-run-id",
        "12345",
        "--output-dir",
        str(output),
    )
    assert result.returncode == 0, result.stderr

    binding = json.loads(
        (output / "web-admin-supply-chain-binding.json").read_text(encoding="utf-8")
    )
    sbom = json.loads(
        (output / "web-admin-sbom.spdx.json").read_text(encoding="utf-8")
    )
    provenance = json.loads(
        (output / "web-admin-slsa-provenance.json").read_text(encoding="utf-8")
    )
    assert binding["source_commit"] == COMMIT
    assert binding["workflow_run_id"] == "12345"
    assert binding["node_base_digest"] == BASE_DIGEST
    assert binding["artifact"]["sha256"] == (
        "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    )
    assert sbom["spdxVersion"] == "SPDX-2.3"
    assert any(
        package["SPDXID"] == "SPDXRef-Package-NodeBase"
        and package["versionInfo"] == BASE_DIGEST
        for package in sbom["packages"]
    )
    dependencies = provenance["predicate"]["buildDefinition"][
        "resolvedDependencies"
    ]
    assert any(item["uri"] == f"node@{BASE_DIGEST}" for item in dependencies)


def test_web_supply_chain_binding_rejects_mutable_or_mismatched_base(tmp_path: Path) -> None:
    artifact = tmp_path / "web.tar.gz"
    artifact.write_bytes(b"image")
    result = run(
        "scripts/write_web_supply_chain_binding.py",
        "--artifact",
        str(artifact),
        "--image-ref",
        "image:test",
        "--image-id",
        "image-id",
        "--node-base-name",
        "node:22-bookworm-slim",
        "--node-base-image",
        "node:22-bookworm-slim",
        "--node-base-digest",
        BASE_DIGEST,
        "--source-commit",
        COMMIT,
        "--repository",
        "Omniorganization/omni-desk-ai",
        "--workflow-run-id",
        "12345",
        "--output-dir",
        str(tmp_path / "out"),
    )
    assert result.returncode != 0
    assert "immutable" in result.stderr


def test_commit_bound_score_binds_checks_coverage_commit_run_and_payload(tmp_path: Path) -> None:
    protection = json.loads(
        (ROOT / ".github/branch-protection.required.json").read_text(encoding="utf-8")
    )
    required = protection["required_check_contexts"]
    checks = {
        "check_runs": [
            {"name": name, "status": "completed", "conclusion": "success"}
            for name in required
        ]
    }
    checks_path = tmp_path / "checks.json"
    checks_path.write_text(json.dumps(checks), encoding="utf-8")
    coverage = {
        "totals": {"percent_covered": 96.0},
        "files": {
            "omnidesk_agent/security/resource_guard.py": {
                "summary": {"percent_covered": 97.0}
            },
            "omnidesk_agent/security/admin_auth.py": {
                "summary": {"percent_covered": 92.0}
            },
            "omnidesk_agent/appsync/chat_service.py": {
                "summary": {"percent_covered": 94.0}
            },
            "omnidesk_agent/models/provider_streaming.py": {
                "summary": {"percent_covered": 93.0}
            },
        },
    }
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
    output = tmp_path / "score.json"

    result = run(
        "scripts/write_commit_bound_industrial_score.py",
        "--root",
        str(ROOT),
        "--repository",
        "Omniorganization/omni-desk-ai",
        "--source-commit",
        COMMIT,
        "--workflow-run-id",
        "67890",
        "--checks-json",
        str(checks_path),
        "--coverage-json",
        str(coverage_path),
        "--output",
        str(output),
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    expected = report.pop("payload_sha256")
    actual = "sha256:" + hashlib.sha256(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert expected == actual
    assert report["source_commit"] == COMMIT
    assert report["workflow_run_id"] == "67890"
    assert report["real_ga_external_evidence"]["excluded_from_source_score"] is True
    assert report["evidence"]["required_checks"]["all_required_success"] is True
    assert 0 <= report["scores"]["source_industrial_maturity"] <= 100


def test_commit_bound_score_rejects_short_commit(tmp_path: Path) -> None:
    checks_path = tmp_path / "checks.json"
    checks_path.write_text('{"check_runs": []}', encoding="utf-8")
    result = run(
        "scripts/write_commit_bound_industrial_score.py",
        "--root",
        str(ROOT),
        "--repository",
        "Omniorganization/omni-desk-ai",
        "--source-commit",
        "abc123",
        "--workflow-run-id",
        "1",
        "--checks-json",
        str(checks_path),
        "--output",
        str(tmp_path / "score.json"),
    )
    assert result.returncode != 0
    assert "full Git SHA" in result.stderr
