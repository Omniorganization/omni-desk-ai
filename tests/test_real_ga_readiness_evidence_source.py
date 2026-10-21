"""Real GA Readiness imports external evidence only through the reviewed fetch helper (H3)."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from scripts import (
    check_real_ga_readiness_contract,
    check_security_workflow_policy,
    fetch_ci_evidence_artifact,
)

ROOT = Path(__file__).resolve().parents[1]
REPO = "Omniorganization/omni-desk-ai"
AUDITED_SHA = "a" * 40
MAIN_VERIFICATION_SHA = "b" * 40
CURRENT_RUN_ID = "4242"
READINESS_WORKFLOW = ".github/workflows/real-ga-readiness.yml"


def _run(**overrides) -> dict:
    run = {
        "id": 77,
        "path": ".github/workflows/main-verification.yml",
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": MAIN_VERIFICATION_SHA,
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
    }
    run.update(overrides)
    return run


def _write_bundle(target: Path) -> None:
    """A control-plane external-ga-evidence artifact with a planted conftest.py."""
    payload = b"transcript"
    (target / "model").mkdir(parents=True)
    (target / "model" / "transcript.txt").write_bytes(payload)
    doc = {"status": "passed", "artifacts": [{"path": "model/transcript.txt", "sha256": hashlib.sha256(payload).hexdigest()}]}
    (target / "model" / "model-live-smoke.json").write_text(json.dumps(doc), encoding="utf-8")
    (target / "control-plane").mkdir()
    (target / "control-plane" / "conftest.py").write_text("import os\n", encoding="utf-8")
    (target / "notes.json").write_text("{}", encoding="utf-8")


def _fetch(
    tmp_path: Path,
    *,
    run_id: str,
    consumer: str = "real-ga-readiness",
    run: dict | None = None,
    ancestors: tuple[str, ...] = (MAIN_VERIFICATION_SHA,),
) -> tuple[dict, list[str]]:
    calls: list[str] = []

    def get_run(requested: str) -> dict:
        calls.append(f"get_run:{requested}")
        return run or _run(id=int(requested))

    def download(target: Path) -> None:
        calls.append("download")
        _write_bundle(target)

    report = fetch_ci_evidence_artifact.fetch(
        consumer=consumer,
        run_id=run_id,
        artifact_name="external-ga-evidence",
        dest=tmp_path / "workspace" / "release" / "external-evidence",
        repository=REPO,
        consumer_sha=AUDITED_SHA,
        policy=fetch_ci_evidence_artifact.load_policy(),
        get_run=get_run,
        is_ancestor=lambda ancestor, _descendant: ancestor in ancestors,
        download=download,
        current_run_id=CURRENT_RUN_ID,
    )
    return report, calls


# --- evidence source policy ---------------------------------------------------


def test_readiness_consumer_accepts_only_main_verification_on_main() -> None:
    spec = fetch_ci_evidence_artifact.consumer_policy(fetch_ci_evidence_artifact.load_policy(), "real-ga-readiness")
    assert spec["workflows"] == [".github/workflows/main-verification.yml"]
    assert spec["events"] == ["workflow_dispatch"]
    assert spec["head_branch"] == "main"
    assert spec["head_sha"] == "ancestor"
    assert spec["allow_same_run"] is True
    assert check_security_workflow_policy.EVIDENCE_CONSUMER_WORKFLOWS["real-ga-readiness.yml"] == "real-ga-readiness"


def test_only_readiness_may_skip_the_source_run_check() -> None:
    policy = fetch_ci_evidence_artifact.load_policy()
    same_run = sorted(name for name, spec in policy["consumers"].items() if spec.get("allow_same_run"))
    assert same_run == ["real-ga-readiness"]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"path": ".github/workflows/real-ga-evidence-control-plane.yml"}, "not a registered producer"),
        ({"path": ".github/workflows/remote-evidence-pipeline.yml"}, "not a registered producer"),
        ({"event": "pull_request"}, "event pull_request is not allowed"),
        ({"event": "push"}, "event push is not allowed"),
        ({"head_branch": "attacker-branch"}, "head_branch must be main"),
        ({"head_repository": {"full_name": "attacker/omni-desk-ai"}}, "head_repository must be"),
        ({"conclusion": "failure"}, "must have completed successfully"),
        ({"status": "in_progress", "conclusion": None}, "must have completed successfully"),
        ({"head_sha": "c" * 40}, "not an ancestor"),
    ],
)
def test_dispatched_readiness_rejects_untrusted_runs_before_download(tmp_path: Path, overrides: dict, expected: str) -> None:
    report, calls = _fetch(tmp_path, run_id="77", run=_run(**overrides))
    assert report["status"] == "failed"
    assert any(expected in issue for issue in report["issues"]), report["issues"]
    assert calls == ["get_run:77"]
    assert not (tmp_path / "workspace").exists()


def test_dispatched_readiness_accepts_ancestor_main_verification_run(tmp_path: Path) -> None:
    report, calls = _fetch(tmp_path, run_id="77")
    assert report["status"] == "passed", report
    assert report["same_run"] is False
    assert report["source_run"]["path"] == ".github/workflows/main-verification.yml"
    assert calls == ["get_run:77", "download"]


# --- same-run (control plane workflow_call) -------------------------------------


def test_same_run_skips_run_check_but_copies_only_expected_evidence(tmp_path: Path) -> None:
    report, calls = _fetch(tmp_path, run_id=CURRENT_RUN_ID)
    dest = tmp_path / "workspace" / "release" / "external-evidence"

    assert report["status"] == "passed", report
    assert report["same_run"] is True
    assert report["source_run"] == {"id": int(CURRENT_RUN_ID)}
    assert calls == ["download"]  # the in-progress run's metadata is not consulted
    assert report["copied_files"] == ["model/model-live-smoke.json", "model/transcript.txt"]
    assert sorted(report["ignored_files"]) == ["control-plane/conftest.py", "notes.json"]
    assert sorted(path.relative_to(dest).as_posix() for path in dest.rglob("*") if path.is_file()) == report["copied_files"]


def test_same_run_still_refuses_code_referenced_as_evidence(tmp_path: Path) -> None:
    def download(target: Path) -> None:
        (target / "model").mkdir(parents=True)
        (target / "model" / "conftest.py").write_text("import os\n", encoding="utf-8")
        doc = {"status": "passed", "artifacts": [{"path": "model/conftest.py", "sha256": "0" * 64}]}
        (target / "model" / "model-live-smoke.json").write_text(json.dumps(doc), encoding="utf-8")

    report = fetch_ci_evidence_artifact.fetch(
        consumer="real-ga-readiness",
        run_id=CURRENT_RUN_ID,
        artifact_name="external-ga-evidence",
        dest=tmp_path / "workspace" / "release" / "external-evidence",
        repository=REPO,
        consumer_sha=AUDITED_SHA,
        policy=fetch_ci_evidence_artifact.load_policy(),
        get_run=lambda _run_id: pytest.fail("same-run fetch must not look up run metadata"),
        is_ancestor=lambda _a, _d: False,
        download=download,
        current_run_id=CURRENT_RUN_ID,
    )
    assert report["status"] == "failed"
    assert any("refusing to copy code file" in issue for issue in report["issues"]), report["issues"]
    assert not (tmp_path / "workspace" / "release" / "external-evidence" / "model").exists()


def test_same_run_rejects_artifacts_without_expected_evidence(tmp_path: Path) -> None:
    report = fetch_ci_evidence_artifact.fetch(
        consumer="real-ga-readiness",
        run_id=CURRENT_RUN_ID,
        artifact_name="external-ga-evidence",
        dest=tmp_path / "dest",
        repository=REPO,
        consumer_sha=AUDITED_SHA,
        policy=fetch_ci_evidence_artifact.load_policy(),
        get_run=lambda _run_id: pytest.fail("same-run fetch must not look up run metadata"),
        is_ancestor=lambda _a, _d: False,
        download=lambda target: (target / "conftest.py").write_text("import os\n", encoding="utf-8"),
        current_run_id=CURRENT_RUN_ID,
    )
    assert report["status"] == "failed"
    assert report["copied_files"] == []


def test_another_in_progress_run_is_not_treated_as_same_run(tmp_path: Path) -> None:
    run = _run(id=4243, path=".github/workflows/real-ga-evidence-control-plane.yml", status="in_progress", conclusion=None)
    report, calls = _fetch(tmp_path, run_id="4243", run=run)
    assert report["status"] == "failed"
    assert report["same_run"] is False
    assert calls == ["get_run:4243"]


@pytest.mark.parametrize("consumer", ["release", "main-verification", "remote-evidence-pipeline", "real-ga-evidence-control-plane"])
def test_same_run_is_not_honoured_for_other_consumers(tmp_path: Path, consumer: str) -> None:
    run = _run(id=int(CURRENT_RUN_ID), status="in_progress", conclusion=None)
    report, calls = _fetch(tmp_path, run_id=CURRENT_RUN_ID, consumer=consumer, run=run)
    assert report["status"] == "failed"
    assert report["same_run"] is False
    assert any("must have completed successfully" in issue for issue in report["issues"]), report["issues"]
    assert calls == [f"get_run:{CURRENT_RUN_ID}"]


def test_policy_rejects_non_boolean_allow_same_run() -> None:
    policy = fetch_ci_evidence_artifact.load_policy()
    policy["consumers"]["real-ga-readiness"]["allow_same_run"] = "true"
    with pytest.raises(ValueError, match="allow_same_run"):
        fetch_ci_evidence_artifact.consumer_policy(policy, "real-ga-readiness")


def test_cli_takes_the_current_run_id_only_from_github_run_id(tmp_path: Path, monkeypatch, capsys) -> None:
    looked_up: list[str] = []

    def gh_run(_repository: str, run_id: str) -> dict:
        looked_up.append(run_id)
        return _run(id=int(run_id), path=".github/workflows/real-ga-evidence-control-plane.yml", status="in_progress", conclusion=None)

    monkeypatch.setattr(fetch_ci_evidence_artifact, "_gh_run", gh_run)
    monkeypatch.setattr(fetch_ci_evidence_artifact, "_gh_is_ancestor", lambda _repository: lambda _a, _d: True)
    monkeypatch.setattr(fetch_ci_evidence_artifact, "_gh_download", lambda _repo, _run_id, _name, target: _write_bundle(target))
    monkeypatch.setenv("GITHUB_RUN_ID", CURRENT_RUN_ID)

    def main(run_id: str, dest: str) -> int:
        return fetch_ci_evidence_artifact.main(
            [
                "--consumer", "real-ga-readiness",
                "--run-id", run_id,
                "--artifact-name", "external-ga-evidence",
                "--dest", str(tmp_path / dest),
                "--repository", REPO,
                "--consumer-sha", AUDITED_SHA,
                "--write-report", str(tmp_path / f"{dest}.json"),
            ]
        )

    assert main(CURRENT_RUN_ID, "same") == 0
    assert looked_up == []
    assert json.loads((tmp_path / "same.json").read_text(encoding="utf-8"))["same_run"] is True

    assert main("4243", "other") == 1
    assert looked_up == ["4243"]
    assert not (tmp_path / "other").exists()

    monkeypatch.delenv("GITHUB_RUN_ID")
    assert main(CURRENT_RUN_ID, "unset") == 1
    assert looked_up == ["4243", CURRENT_RUN_ID]
    capsys.readouterr()


# --- workflow and policy scripts ------------------------------------------------


def test_readiness_workflow_uses_fetch_helper() -> None:
    workflow = (ROOT / READINESS_WORKFLOW).read_text(encoding="utf-8")
    assert "gh run download" not in workflow
    assert "rglob" not in workflow
    assert "dist/imported-external-evidence" not in workflow
    assert "python scripts/fetch_ci_evidence_artifact.py \\\n            --consumer real-ga-readiness" in workflow
    assert "--dest release/external-evidence" in workflow
    assert "dist/evidence/external-evidence-fetch.json" in workflow

    control_plane = (ROOT / ".github/workflows/real-ga-evidence-control-plane.yml").read_text(encoding="utf-8")
    assert "uses: ./.github/workflows/real-ga-readiness.yml" in control_plane
    assert "external_evidence_run_id: ${{ github.run_id }}" in control_plane


def _copy_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel in (".github", "scripts", "docs", "release"):
        shutil.copytree(ROOT / rel, root / rel, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "pyproject.toml", root)
    return root


def _restore_gh_run_download(root: Path) -> None:
    path = root / READINESS_WORKFLOW
    text = path.read_text(encoding="utf-8")
    old = (
        "python scripts/fetch_ci_evidence_artifact.py \\\n"
        "            --consumer real-ga-readiness \\\n"
        '            --run-id "$EXTERNAL_EVIDENCE_RUN_ID" \\\n'
    )
    assert old in text
    path.write_text(text.replace(old, 'gh run download "$EXTERNAL_EVIDENCE_RUN_ID" \\\n'), encoding="utf-8")


def test_policy_scripts_pass_current_tree() -> None:
    assert check_security_workflow_policy.release_chain_issues(ROOT) == []
    report = check_real_ga_readiness_contract.audit(ROOT)
    assert report["status"] == "passed", report["failures"]


def test_policy_scripts_reject_gh_run_download_in_readiness(tmp_path: Path) -> None:
    root = _copy_tree(tmp_path)
    assert check_real_ga_readiness_contract.audit(root)["status"] == "passed"
    _restore_gh_run_download(root)

    issues = check_security_workflow_policy.release_chain_issues(root)
    assert "real-ga-readiness.yml: evidence must be downloaded with scripts/fetch_ci_evidence_artifact.py, not gh run download" in issues
    assert "real-ga-readiness.yml: evidence downloads must use scripts/fetch_ci_evidence_artifact.py --consumer real-ga-readiness" in issues

    failures = check_real_ga_readiness_contract.audit(root)["failures"]
    assert "real-ga-readiness workflow missing snippet: --consumer real-ga-readiness" in failures
    assert any("not gh run download" in failure for failure in failures), failures


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda consumers: consumers.pop("real-ga-readiness"),
            "missing consumer real-ga-readiness",
        ),
        (
            lambda consumers: consumers["release"].update(allow_same_run=True),
            "consumer release must not skip the source run check (allow_same_run)",
        ),
        (
            lambda consumers: consumers["main-verification"].update(allow_same_run=True),
            "consumer main-verification must not skip the source run check (allow_same_run)",
        ),
        (
            lambda consumers: consumers["real-ga-readiness"].update(head_branch="develop"),
            "consumer real-ga-readiness must require head_branch main",
        ),
        (
            lambda consumers: consumers["real-ga-readiness"]["events"].append("pull_request"),
            "consumer real-ga-readiness must not accept pull request runs",
        ),
    ],
)
def test_security_policy_rejects_weakened_readiness_sources(tmp_path: Path, mutate, expected: str) -> None:
    root = _copy_tree(tmp_path)
    policy_path = root / "release" / "evidence-source-workflows.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    mutate(policy["consumers"])
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    issues = check_security_workflow_policy.release_chain_issues(root)
    assert any(expected in issue for issue in issues), issues
