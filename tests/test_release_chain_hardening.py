"""Release chain: evidence imports, signing order, signer identity and secret scope."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from scripts import (
    check_security_workflow_policy,
    check_supply_chain_standard,
    check_workflow_governance,
    fetch_ci_evidence_artifact,
    finalize_release_payload,
    verify_release_artifact,
    verify_release_signatures,
)
from scripts.list_cosign_artifacts import list_payload_artifacts

ROOT = Path(__file__).resolve().parents[1]
REPO = "Omniorganization/omni-desk-ai"
MAIN_SHA = "a" * 40
OLDER_MAIN_SHA = "b" * 40


# --- pytest collection (H3) -------------------------------------------------


def test_pytest_does_not_import_conftest_from_downloaded_artifacts(tmp_path: Path) -> None:
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    planted = {
        "release/external-evidence/control-plane/conftest.py": "evidence",
        "release/conftest.py": "release",
        "dist/conftest.py": "dist",
        "current/conftest.py": "current",
        "previous/conftest.py": "previous",
    }
    for rel, marker in planted.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"from pathlib import Path\nPath({str(tmp_path / ('marker-' + marker))!r}).write_text('executed')\n",
            encoding="utf-8",
        )
        # A test module next to it must not be collected either.
        (path.parent / "test_planted.py").write_text("def test_planted():\n    raise AssertionError\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert sorted(path.name for path in tmp_path.glob("marker-*")) == []


# --- evidence source runs (H3) ----------------------------------------------


def _run(**overrides) -> dict:
    run = {
        "id": 42,
        "path": ".github/workflows/main-verification.yml",
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": MAIN_SHA,
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
    }
    run.update(overrides)
    return run


def _validate(run: dict, consumer: str = "release", ancestors: tuple[str, ...] = ()) -> list[str]:
    spec = fetch_ci_evidence_artifact.consumer_policy(fetch_ci_evidence_artifact.load_policy(), consumer)
    return fetch_ci_evidence_artifact.validate_source_run(
        run,
        spec,
        repository=REPO,
        consumer_sha=MAIN_SHA,
        is_ancestor=lambda ancestor, _descendant: ancestor in ancestors,
    )


def test_release_accepts_exact_commit_main_verification_run() -> None:
    assert _validate(_run()) == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"path": ".github/workflows/ci.yml"}, "not a registered producer"),
        ({"event": "pull_request"}, "event pull_request is not allowed"),
        ({"head_branch": "attacker-branch"}, "head_branch must be main"),
        ({"head_repository": {"full_name": "attacker/omni-desk-ai"}}, "head_repository must be"),
        ({"conclusion": "failure"}, "must have completed successfully"),
        ({"status": "in_progress", "conclusion": None}, "must have completed successfully"),
        ({"head_sha": OLDER_MAIN_SHA}, "must equal the consuming commit"),
        ({"head_sha": "not-a-sha"}, "missing or malformed"),
    ],
)
def test_release_rejects_untrusted_evidence_runs(overrides: dict, expected: str) -> None:
    issues = _validate(_run(**overrides))
    assert any(expected in issue for issue in issues), issues


def test_ancestor_policy_requires_main_history() -> None:
    run = _run(path=".github/workflows/real-ga-evidence-control-plane.yml", head_sha=OLDER_MAIN_SHA)
    assert _validate(run, "main-verification", ancestors=(OLDER_MAIN_SHA,)) == []
    issues = _validate(run, "main-verification", ancestors=())
    assert any("not an ancestor" in issue for issue in issues)


def test_every_evidence_consumer_has_a_policy() -> None:
    policy = fetch_ci_evidence_artifact.load_policy()
    for consumer in check_security_workflow_policy.EVIDENCE_CONSUMER_WORKFLOWS.values():
        spec = fetch_ci_evidence_artifact.consumer_policy(policy, consumer)
        assert spec["head_branch"] == "main"
        assert "pull_request" not in spec["events"]
    assert policy["consumers"]["release"]["head_sha"] == "exact"


def _model_evidence(root: Path, artifact_rel: str, payload: bytes) -> None:
    (root / "model").mkdir(parents=True, exist_ok=True)
    (root / artifact_rel).write_bytes(payload)
    doc = {
        "status": "passed",
        "artifacts": [{"path": artifact_rel, "sha256": hashlib.sha256(payload).hexdigest()}],
    }
    (root / "model" / "model-live-smoke.json").write_text(json.dumps(doc), encoding="utf-8")


def _fetch(tmp_path: Path, write_artifact, run: dict | None = None) -> tuple[dict, list[Path]]:
    downloads: list[Path] = []

    def download(target: Path) -> None:
        downloads.append(target)
        write_artifact(target)

    report = fetch_ci_evidence_artifact.fetch(
        consumer="release",
        run_id="42",
        artifact_name="external-ga-evidence-bound",
        dest=tmp_path / "workspace" / "release" / "external-evidence",
        repository=REPO,
        consumer_sha=MAIN_SHA,
        policy=fetch_ci_evidence_artifact.load_policy(),
        get_run=lambda _run_id: run or _run(),
        is_ancestor=lambda _a, _d: False,
        download=download,
    )
    return report, downloads


def test_fetch_copies_only_expected_evidence_out_of_quarantine(tmp_path: Path) -> None:
    def write_artifact(target: Path) -> None:
        evidence = target / "release" / "external-evidence"
        _model_evidence(evidence, "model/transcript.txt", b"transcript")
        (evidence / "control-plane").mkdir()
        (evidence / "control-plane" / "conftest.py").write_text("import os\n", encoding="utf-8")
        (evidence / "conftest.py").write_text("import os\n", encoding="utf-8")

    report, downloads = _fetch(tmp_path, write_artifact)
    dest = tmp_path / "workspace" / "release" / "external-evidence"

    assert report["status"] == "passed", report
    assert report["copied_files"] == ["model/model-live-smoke.json", "model/transcript.txt"]
    assert "control-plane/conftest.py" in report["ignored_files"]
    assert not (dest / "control-plane" / "conftest.py").exists()
    assert not (dest / "conftest.py").exists()
    assert not downloads[0].exists()  # the quarantine directory is removed
    assert not str(downloads[0]).startswith(str(tmp_path / "workspace"))


def test_fetch_refuses_code_referenced_as_evidence(tmp_path: Path) -> None:
    report, _ = _fetch(tmp_path, lambda target: _model_evidence(target, "model/conftest.py", b"import os\n"))
    assert report["status"] == "failed"
    assert any("refusing to copy code file" in issue for issue in report["issues"])
    assert not (tmp_path / "workspace" / "release" / "external-evidence" / "model").exists()


def test_fetch_does_not_download_from_an_untrusted_run(tmp_path: Path) -> None:
    report, downloads = _fetch(tmp_path, lambda target: None, run=_run(head_branch="attacker-branch"))
    assert report["status"] == "failed"
    assert downloads == []


def test_fetch_rejects_artifacts_without_expected_evidence(tmp_path: Path) -> None:
    report, _ = _fetch(tmp_path, lambda target: (target / "conftest.py").write_text("import os\n", encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["copied_files"] == []


# --- signing order (M16 / non-security ordering) ------------------------------


def _keys() -> tuple[str, str]:
    key = ed25519.Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


def _base_payload(dist: Path) -> None:
    dist.mkdir()
    wheel = dist / "omnidesk_agent-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("omnidesk_agent-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: omnidesk-agent\nVersion: 1.0\n")
    (dist / "sbom.json").write_text('{"schema_version": "omnidesk-lockfile-sbom/v1"}', encoding="utf-8")
    metadata = {
        "version": "1.0",
        "artifact": {"sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
        "sbom": {"name": "sbom.json", "sha256": hashlib.sha256((dist / "sbom.json").read_bytes()).hexdigest()},
        "image": {"digest": "sha256:" + "c" * 64},
    }
    (dist / "release_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")


def _post_build_payload(dist: Path) -> set[str]:
    """What release.yml writes after build_release.sh, in workflow order."""
    (dist / "tri-app-native-artifacts.json").write_text("{}", encoding="utf-8")
    (dist / "tri-app-native-artifacts.tar.gz").write_bytes(b"native")
    (dist / "values.production.locked.yaml").write_text("image: digest\n", encoding="utf-8")
    (dist / "slsa-provenance.json").write_text("{}", encoding="utf-8")
    cosign_payload = {path.name for path in list_payload_artifacts(dist)}
    for name in cosign_payload:
        (dist / f"{name}.cosign.sig").write_text("sig", encoding="utf-8")
        (dist / f"{name}.cosign.pem").write_text("cert", encoding="utf-8")
    for name in ("sbom.intoto", "slsa.intoto"):
        (dist / f"{name}.sig").write_text("sig", encoding="utf-8")
        (dist / f"{name}.pem").write_text("cert", encoding="utf-8")
    return cosign_payload


@pytest.fixture
def ed25519_env(monkeypatch):
    for name in (
        "OMNIDESK_RELEASE_SIGNING_KEY",
        "OMNIDESK_RELEASE_SIGNATURE_ALGORITHM",
    ):
        monkeypatch.delenv(name, raising=False)
    private, public = _keys()
    monkeypatch.setenv("OMNIDESK_RELEASE_SIGNING_ED25519_KEY", private)
    monkeypatch.setenv("OMNIDESK_RELEASE_VERIFY_PUBLIC_KEY", public)
    monkeypatch.setenv("OMNIDESK_REQUIRE_ED25519_RELEASE_SIGNATURES", "1")
    return monkeypatch


def test_finalized_release_payload_verifies_with_require_signatures(tmp_path: Path, ed25519_env) -> None:
    dist = tmp_path / "dist"
    _base_payload(dist)
    cosign_payload = _post_build_payload(dist)

    assert finalize_release_payload.main([str(dist), "--expected-image-digest", "sha256:" + "c" * 64]) == 0

    # What staging, production and rollback run on the downloaded artifact.
    ed25519_env.delenv("OMNIDESK_RELEASE_SIGNING_ED25519_KEY")
    assert verify_release_artifact.main([str(dist), "--expected-version", "1.0", "--require-signatures", "--require-metadata"]) == 0
    assert verify_release_signatures.main(str(dist)) == 0
    # Verifiers look for exactly the Cosign signatures the release job produced.
    assert {path.name for path in list_payload_artifacts(dist)} == cosign_payload
    signed = {item["name"] for item in json.loads((dist / "release_signatures.json").read_text())["artifacts"]}
    assert {"values.production.locked.yaml", "slsa-provenance.json", "slsa.intoto.pem", "checksums.txt"} <= signed


def test_payload_written_after_signing_fails_require_signatures(tmp_path: Path, ed25519_env) -> None:
    # The pre-fix order: Helm values rendered after the release was signed.
    dist = tmp_path / "dist"
    _base_payload(dist)
    assert finalize_release_payload.main([str(dist)]) == 0
    (dist / "values.production.locked.yaml").write_text("image: digest\n", encoding="utf-8")
    assert verify_release_artifact.main([str(dist), "--require-signatures", "--require-metadata"]) == 1
    assert verify_release_signatures.main(str(dist)) == 1


def test_finalize_refuses_hmac_when_ed25519_is_required(tmp_path: Path, ed25519_env) -> None:
    dist = tmp_path / "dist"
    _base_payload(dist)
    ed25519_env.setenv("OMNIDESK_RELEASE_SIGNATURE_ALGORITHM", "hmac-sha256")
    ed25519_env.setenv("OMNIDESK_RELEASE_SIGNING_KEY", "legacy-secret")
    assert finalize_release_payload.main([str(dist)]) != 0
    assert not (dist / "release_signatures.json").exists()


# --- governance scripts (H2, M15, M16, H3) ------------------------------------


def _copy_release_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(ROOT / ".github" / "workflows", root / ".github" / "workflows")
    (root / "release").mkdir()
    shutil.copy(ROOT / "release" / "evidence-source-workflows.json", root / "release")
    shutil.copy(ROOT / "pyproject.toml", root)
    return root


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_release_chain_policy_passes_current_tree() -> None:
    assert check_security_workflow_policy.release_chain_issues(ROOT) == []
    assert check_workflow_governance.main([str(ROOT), "--require-real-workflows"]) == 0
    assert check_supply_chain_standard.main([str(ROOT)]) == 0


@pytest.mark.parametrize(
    ("workflow", "old", "new", "expected"),
    [
        (
            "release.yml",
            "    if: github.ref == 'refs/heads/main'\n    runs-on: ubuntu-latest\n    environment: release\n    env:",
            "    if: github.ref == 'refs/heads/main' || startsWith(github.ref, 'refs/tags/v')\n    runs-on: ubuntu-latest\n    environment: release\n    env:",
            "build job must run only if",
        ),
        (
            "release.yml",
            "    runs-on: ubuntu-latest\n    environment: release\n    env:",
            "    runs-on: ubuntu-latest\n    env:",
            "outside the protected release environment",
        ),
        (
            "release.yml",
            "    runs-on: macos-latest\n    environment: release\n",
            "    runs-on: macos-latest\n",
            "job mobile-ios-release reads release signing secrets",
        ),
        (
            "deploy-staging.yml",
            ' --source-ref "$RELEASE_SOURCE_REF"',
            "",
            "must pin --source-ref refs/heads/main",
        ),
        (
            "supply-chain.yml",
            "release\\.yml@refs/heads/main$'",
            "release\\.yml@refs/(heads/main|tags/v.+)$'",
            "must only accept release.yml@refs/heads/main",
        ),
        (
            "promote-production.yml",
            '          OMNIDESK_REQUIRE_ED25519_RELEASE_SIGNATURES: "1"\n',
            "          OMNIDESK_RELEASE_SIGNING_KEY: ${{ secrets.OMNIDESK_RELEASE_SIGNING_KEY }}\n",
            "must not use secrets.OMNIDESK_RELEASE_SIGNING_KEY",
        ),
        (
            "rollback-drill.yml",
            '          OMNIDESK_REQUIRE_ED25519_RELEASE_SIGNATURES: "1"\n',
            "",
            "OMNIDESK_REQUIRE_ED25519_RELEASE_SIGNATURES=1",
        ),
        (
            "main-verification.yml",
            "python scripts/fetch_ci_evidence_artifact.py \\\n              --consumer main-verification",
            'gh run download "$EXTERNAL_EVIDENCE_RUN_ID" \\\n              --consumer main-verification',
            "not gh run download",
        ),
    ],
)
def test_release_chain_policy_rejects_regressions(tmp_path: Path, workflow: str, old: str, new: str, expected: str) -> None:
    root = _copy_release_tree(tmp_path)
    _edit(root / ".github" / "workflows" / workflow, old, new)
    issues = check_security_workflow_policy.release_chain_issues(root)
    assert any(expected in issue for issue in issues), issues


def test_release_chain_policy_requires_pytest_testpaths(tmp_path: Path) -> None:
    root = _copy_release_tree(tmp_path)
    _edit(root / "pyproject.toml", 'testpaths = ["tests"]\n', "")
    issues = check_security_workflow_policy.release_chain_issues(root)
    assert any("testpaths" in issue for issue in issues), issues


def test_release_chain_policy_rejects_pull_request_evidence_sources(tmp_path: Path) -> None:
    root = _copy_release_tree(tmp_path)
    policy_path = root / "release" / "evidence-source-workflows.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["consumers"]["main-verification"]["events"].append("pull_request")
    policy["consumers"]["release"]["head_sha"] = "ancestor"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    issues = check_security_workflow_policy.release_chain_issues(root)
    assert any("must not accept pull request runs" in issue for issue in issues), issues
    assert any("exact release commit" in issue for issue in issues), issues


def _release_governance_issues(tmp_path: Path, old: str, new: str) -> list[str]:
    root = _copy_release_tree(tmp_path)
    _edit(root / ".github" / "workflows" / "release.yml", old, new)
    return check_workflow_governance.check(root, require_real_workflows=True)


def test_workflow_governance_rejects_payload_written_after_signing(tmp_path: Path) -> None:
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    start = release.index("      - name: Render locked Helm production values\n")
    end = release.index("      - name: Write SLSA provenance for the release payload\n")
    helm_step = release[start:end]
    finalize_end = release.index("      - run: python scripts/verify_release_artifact.py dist --expected-version")
    reordered = release[:start] + release[end:finalize_end] + helm_step + release[finalize_end:]
    root = _copy_release_tree(tmp_path)
    (root / ".github" / "workflows" / "release.yml").write_text(reordered, encoding="utf-8")
    issues = check_workflow_governance.check(root, require_real_workflows=True)
    assert any("before scripts/finalize_release_payload.py dist" in issue or "after signing" in issue for issue in issues), issues


def test_workflow_governance_rejects_direct_evidence_download(tmp_path: Path) -> None:
    issues = _release_governance_issues(
        tmp_path,
        "python scripts/fetch_ci_evidence_artifact.py \\\n            --consumer release",
        'gh run download "$EXTERNAL_EVIDENCE_RUN_ID" \\\n            --consumer release',
    )
    assert any("not gh run download" in issue for issue in issues), issues


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (
            "--report-path reports/release/web-admin-preflight.json",
            "--report-path dist/web-admin-preflight.json",
            "uses dist/ before bash scripts/build_release.sh",
        ),
        (
            "--write-live-report reports/release/main-verification-live-artifact.json",
            "--write-live-report dist/main-verification-live-artifact.json",
            "uses dist/ before bash scripts/build_release.sh",
        ),
        (
            "cp release/real-ga-evidence-summary-1.12.7.json reports/release/",
            "cp release/real-ga-evidence-summary-1.12.7.json dist/",
            "uses dist/ before bash scripts/build_release.sh",
        ),
        (
            "          name: real-ga-evidence-import-reports\n          if-no-files-found: error\n",
            "          name: real-ga-evidence-import-reports\n",
            "must set if-no-files-found: error",
        ),
        (
            "            reports/release/main-verification-live-artifact.json\n",
            "",
            "upload missing reports/release/main-verification-live-artifact.json",
        ),
        (
            "            reports/release/current-release-artifact-binding.json\n",
            "            dist/current-release-artifact-binding.json\n",
            "must not upload from dist/",
        ),
        (
            "            reports/release/main-verification-live-artifact.json \\\n",
            "",
            "report check missing reports/release/main-verification-live-artifact.json",
        ),
    ],
)
def test_workflow_governance_keeps_gate_reports_out_of_the_release_payload(
    tmp_path: Path, old: str, new: str, expected: str
) -> None:
    # build_release.sh starts with rm -rf dist: a gate report written to dist/ before
    # it is silently lost, and dist/ is the signed, attested release payload.
    issues = _release_governance_issues(tmp_path, old, new)
    assert any(expected in issue for issue in issues), issues


def test_workflow_governance_checks_gate_reports_before_the_payload_build(tmp_path: Path) -> None:
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    start = release.index("      - name: Check every Real GA evidence import report was written\n")
    end = release.index("      - name: Verify release channel policy after external evidence gate\n")
    check_step = release[start:end]
    moved = release[:start] + release[end:] + check_step
    root = _copy_release_tree(tmp_path)
    (root / ".github" / "workflows" / "release.yml").write_text(moved, encoding="utf-8")
    issues = check_workflow_governance.check(root, require_real_workflows=True)
    assert any("must check every gate report exists before bash scripts/build_release.sh" in issue for issue in issues), issues


def test_supply_chain_standard_rejects_tag_signer_identity(tmp_path: Path, capsys) -> None:
    root = tmp_path / "repo"
    shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns(".git", "node_modules", "apps", "*.egg-info", "__pycache__"))
    _edit(
        root / ".github" / "workflows" / "promote-production.yml",
        "release\\.yml@refs/heads/main$'",
        "release\\.yml@refs/(heads/main|tags/v.+)$'",
    )
    assert check_supply_chain_standard.main([str(root)]) == 1
    assert "must not trust release tags" in capsys.readouterr().err
