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


# --- attestation signers (L14) -------------------------------------------------

# The removed attestation.yml: re-attested the files of any run id with its own
# main-branch identity, without checking which workflow, branch or fork made them.
UNCHECKED_REATTESTATION_WORKFLOW = """\
name: Artifact Attestation
on:
  workflow_dispatch:
    inputs:
      artifact_run_id:
        required: true
      artifact_name:
        required: true
permissions:
  contents: read
  actions: read
  id-token: write
  attestations: write
jobs:
  attest:
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    steps:
      - name: Download release artifact
        env:
          GH_TOKEN: ${{ github.token }}
          ARTIFACT_RUN_ID: ${{ inputs.artifact_run_id }}
          ARTIFACT_NAME: ${{ inputs.artifact_name }}
        run: gh run download "$ARTIFACT_RUN_ID" --name "$ARTIFACT_NAME" --dir dist
      - uses: actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8
        with:
          subject-path: dist/*
"""

DOWNLOAD_BY_RUN_ID_STEP = """\
      - uses: actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333
        with:
          name: anything
          run-id: ${{ inputs.external_evidence_run_id }}
"""


def _signer_policy_issues(tmp_path: Path, workflow: str, old: str, new: str) -> list[str]:
    root = _copy_release_tree(tmp_path)
    assert check_security_workflow_policy.release_chain_issues(root) == []
    _edit(root / ".github" / "workflows" / workflow, old, new)
    return check_security_workflow_policy.release_chain_issues(root)


def test_release_artifacts_are_attested_only_by_registered_signer_jobs() -> None:
    assert not (ROOT / ".github" / "workflows" / "attestation.yml").exists()
    for name, signer_jobs in check_security_workflow_policy.ATTESTATION_SIGNER_JOBS.items():
        jobs = check_security_workflow_policy._load_workflow(ROOT / ".github" / "workflows" / name)["jobs"]
        for job_id in signer_jobs:
            assert check_security_workflow_policy._mints_attestations(jobs[job_id]), (name, job_id)


def test_release_chain_policy_rejects_unchecked_reattestation_workflow(tmp_path: Path) -> None:
    root = _copy_release_tree(tmp_path)
    (root / ".github" / "workflows" / "attestation.yml").write_text(UNCHECKED_REATTESTATION_WORKFLOW, encoding="utf-8")
    issues = check_security_workflow_policy.release_chain_issues(root)
    assert any("attestation.yml: job attest mints artifact attestations" in issue for issue in issues), issues
    assert any("attestation.yml: attestations: write must be granted only" in issue for issue in issues), issues


@pytest.mark.parametrize(
    "download",
    [
        'gh run download "$ARTIFACT_RUN_ID" --name "$ARTIFACT_NAME" --dir dist',
        # Naming the evidence fetcher is not a check of this run.
        'echo scripts/fetch_ci_evidence_artifact.py; gh run download "$ARTIFACT_RUN_ID" --name "$ARTIFACT_NAME" --dir dist',
    ],
)
def test_release_chain_policy_rejects_reattestation_even_when_registered(
    tmp_path: Path, monkeypatch, download: str
) -> None:
    # Registering the job is not enough: it would still need the release
    # environment and a source-run check before the download.
    monkeypatch.setitem(check_security_workflow_policy.ATTESTATION_SIGNER_JOBS, "attestation.yml", {"attest": ()})
    root = _copy_release_tree(tmp_path)
    workflow = UNCHECKED_REATTESTATION_WORKFLOW.replace(
        'run: gh run download "$ARTIFACT_RUN_ID" --name "$ARTIFACT_NAME" --dir dist', f"run: {download}"
    )
    (root / ".github" / "workflows" / "attestation.yml").write_text(workflow, encoding="utf-8")
    issues = check_security_workflow_policy.release_chain_issues(root)
    assert any("attestation.yml: signer job attest must run in the protected release environment" in issue for issue in issues), issues
    assert any("attestation.yml: job attest downloads artifacts from a run id without first checking" in issue for issue in issues), issues


@pytest.mark.parametrize(
    ("workflow", "old", "new", "expected"),
    [
        (
            "deploy-staging.yml",
            "  attestations: read\n",
            "  attestations: write\n",
            "deploy-staging.yml: attestations: write must be granted only to a registered signer job",
        ),
        (
            "promote-production.yml",
            '          cosign verify "$OMNIDESK_IMAGE_REF@$OMNIDESK_IMAGE_DIGEST"',
            '          cosign sign --yes "$OMNIDESK_IMAGE_REF@$OMNIDESK_IMAGE_DIGEST"\n          cosign verify "$OMNIDESK_IMAGE_REF@$OMNIDESK_IMAGE_DIGEST"',
            "promote-production.yml: job promotion-gate mints",
        ),
        (
            "web-first-release.yml",
            "    if: github.ref == 'refs/heads/main'\n    runs-on: ubuntu-latest\n",
            "    runs-on: ubuntu-latest\n",
            "web-first-release.yml: signer job release must run only if",
        ),
        (
            "web-first-release.yml",
            "    environment: release\n",
            "",
            "web-first-release.yml: signer job release must run in the protected release environment",
        ),
        (
            "web-first-release.yml",
            "on:\n  workflow_dispatch:\n",
            "on:\n  workflow_run:\n    workflows: [Web First Candidate]\n    types: [completed]\n  workflow_dispatch:\n",
            "web-first-release.yml: signer workflow may only be triggered by",
        ),
        (
            "web-first-release.yml",
            "'head_branch': 'main',",
            "",
            "web-first-release.yml: job release downloads artifacts from a run id without first checking",
        ),
        (
            "web-first-release.yml",
            "'conclusion': 'success',",
            "",
            "web-first-release.yml: job release downloads artifacts from a run id without first checking",
        ),
        (
            "web-first-release.yml",
            "'path': '.github/workflows/web-first-candidate.yml'",
            "'path': '.github/workflows/ci.yml'",
            "web-first-release.yml: job release downloads artifacts from a run id without first checking",
        ),
        (
            # A second download needs its own check, also in a later step.
            "web-first-release.yml",
            "      - name: Attest exact scoped artifact digests with GitHub OIDC\n",
            '      - run: gh run download "$CANDIDATE_RUN_ID" --name other --dir reports/web-first\n'
            "      - name: Attest exact scoped artifact digests with GitHub OIDC\n",
            "web-first-release.yml: job release downloads artifacts from a run id without first checking",
        ),
        (
            "release.yml",
            "on:\n  workflow_dispatch:\n",
            "on:\n  pull_request:\n  workflow_dispatch:\n",
            "release.yml: signer workflow may only be triggered by",
        ),
        (
            "release.yml",
            "          pattern: release-*\n",
            "          pattern: release-*\n          run-id: ${{ inputs.external_evidence_run_id }}\n",
            "release.yml: job build downloads artifacts from a run id without first checking",
        ),
        (
            # After the checked evidence fetch, another run's artifact is still unchecked.
            "release.yml",
            "      - name: List release payload for GitHub artifact attestation\n",
            DOWNLOAD_BY_RUN_ID_STEP + "      - name: List release payload for GitHub artifact attestation\n",
            "release.yml: job build downloads artifacts from a run id without first checking",
        ),
        (
            # Sibling jobs feed the signer job through needs and artifacts.
            "release.yml",
            "      - run: npm ci\n",
            "      - run: npm ci\n" + DOWNLOAD_BY_RUN_ID_STEP,
            "release.yml: job web-admin-release downloads artifacts from a run id without first checking",
        ),
    ],
)
def test_release_chain_policy_rejects_attestation_signer_regressions(
    tmp_path: Path, workflow: str, old: str, new: str, expected: str
) -> None:
    issues = _signer_policy_issues(tmp_path, workflow, old, new)
    assert any(expected in issue for issue in issues), issues


def test_release_chain_policy_requires_the_run_check_before_the_download(tmp_path: Path) -> None:
    root = _copy_release_tree(tmp_path)
    path = root / ".github" / "workflows" / "web-first-release.yml"
    text = path.read_text(encoding="utf-8")
    download = '          gh run download "$CANDIDATE_RUN_ID" --name web-first-candidate --dir reports/web-first\n'
    _edit(path, download, "")
    _edit(path, "          mkdir -p reports/web-first\n", "          mkdir -p reports/web-first\n" + download)
    assert path.read_text(encoding="utf-8") != text
    issues = check_security_workflow_policy.release_chain_issues(root)
    assert any("web-first-release.yml: job release downloads artifacts from a run id without first checking" in issue for issue in issues), issues


def _extra_workflow(job: str, permissions: str = "permissions:\n  contents: read\n") -> str:
    return "name: Extra\non:\n  workflow_dispatch:\n" + permissions + "jobs:\n  extra:\n" + job


@pytest.mark.parametrize(
    ("workflow", "expected"),
    [
        (
            _extra_workflow("    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/attest@1c608d11d69870c2092266b3f9a6f3abbf17002c\n"),
            "job extra mints",
        ),
        (
            _extra_workflow("    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/attest-sbom@bd218ad0dbcb3e146bd073d1d9c6d78e08aa8a0b\n"),
            "job extra mints",
        ),
        (
            _extra_workflow("    runs-on: ubuntu-latest\n    steps:\n      - uses: Actions/Attest-Build-Provenance@4d101475d8b20a2381f78447822ac1eab6504dd8\n"),
            "job extra mints",
        ),
        (
            _extra_workflow("    runs-on: ubuntu-latest\n    steps:\n      - uses: sigstore/gh-action-sigstore-python@f7ad0af51a5648d09a20d00370f0a91c3bdf8f84\n"),
            "job extra mints",
        ),
        (
            _extra_workflow(
                "    permissions:\n      id-token: write\n"
                "    uses: slsa-framework/slsa-github-generator/.github/workflows/generator_generic_slsa3.yml@v2.1.0\n"
            ),
            "job extra mints",
        ),
        (_extra_workflow("    runs-on: ubuntu-latest\n    steps:\n      - run: cosign attest-blob --yes --predicate p.json f\n"), "job extra mints"),
        (_extra_workflow("    runs-on: ubuntu-latest\n    steps:\n      - run: cosign --verbose sign-blob --yes f\n"), "job extra mints"),
        (_extra_workflow('    runs-on: ubuntu-latest\n    steps:\n      - run: \'"$(command -v cosign)" sign --yes img\'\n'), "job extra mints"),
        (_extra_workflow("    runs-on: ubuntu-latest\n    steps:\n      - run: python -m sigstore sign f\n"), "job extra mints"),
        (_extra_workflow("    runs-on: ubuntu-latest\n    permissions: write-all\n    steps:\n      - run: true\n"), "job extra mints"),
        (
            _extra_workflow("    runs-on: ubuntu-latest\n    steps:\n      - run: true\n", permissions="permissions: write-all\n"),
            "attestations: write must be granted only to a registered signer job, not workflow-wide",
        ),
    ],
)
@pytest.mark.parametrize("filename", ["extra.yml", "extra.yaml"])
def test_release_chain_policy_rejects_unregistered_signers(tmp_path: Path, filename: str, workflow: str, expected: str) -> None:
    root = _copy_release_tree(tmp_path)
    (root / ".github" / "workflows" / filename).write_text(workflow, encoding="utf-8")
    issues = check_security_workflow_policy.release_chain_issues(root)
    assert any(issue.startswith(f"{filename}: ") and expected in issue for issue in issues), issues


def test_release_chain_policy_allows_verification_outside_signer_jobs(tmp_path: Path) -> None:
    root = _copy_release_tree(tmp_path)
    workflow = _extra_workflow(
        "    runs-on: ubuntu-latest\n    steps:\n"
        "      - run: |\n"
        '          cosign verify-blob --certificate-identity-regexp "$ID" f\n'
        '          cosign verify-attestation --type spdx "$IMG"\n'
        "          gh attestation verify f --repo o/r --signer-workflow o/r/.github/workflows/release.yml --source-ref refs/heads/main\n",
        permissions="permissions:\n  contents: read\n  attestations: read\n  id-token: write\n",
    )
    (root / ".github" / "workflows" / "extra.yml").write_text(workflow, encoding="utf-8")
    assert check_security_workflow_policy.release_chain_issues(root) == []


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
            "        if: always()\n        with:\n          name: real-ga-evidence-import-reports\n",
            "        with:\n          name: real-ga-evidence-import-reports\n",
            "must run if: always()",
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
