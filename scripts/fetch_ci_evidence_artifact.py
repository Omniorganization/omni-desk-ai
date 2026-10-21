#!/usr/bin/env python3
"""Download an evidence artifact from another workflow run, only from a reviewed producer.

A run id is free-form workflow input, and an artifact uploaded by a branch or
fork run can contain anything: forged evidence, or a conftest.py that pytest
would import in a job holding signing permissions. Before downloading, the run
must match the consumer's entry in release/evidence-source-workflows.json:
producer workflow path, trigger event, head branch, this repository (not a
fork), a successful conclusion, and a head commit equal to (or, where the
policy allows, an ancestor of) the consuming commit. The artifact is unpacked
outside the workspace and only the expected evidence JSON files, plus the
artifacts they reference, are copied to --dest.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

try:
    from scripts import import_external_ga_evidence
except ModuleNotFoundError:  # pragma: no cover - supports direct script execution
    import import_external_ga_evidence  # type: ignore[no-redef]


SCHEMA_VERSION = "omnidesk-ci-evidence-fetch/v1"
POLICY_PATH = Path(__file__).resolve().parents[1] / "release" / "evidence-source-workflows.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    policy = json.loads(path.read_text(encoding="utf-8"))
    if policy.get("schema") != "omnidesk-evidence-source-workflows/v1":
        raise ValueError(f"unsupported evidence source policy schema in {path}")
    return policy


def consumer_policy(policy: dict[str, Any], consumer: str) -> dict[str, Any]:
    spec = (policy.get("consumers") or {}).get(consumer)
    if not isinstance(spec, dict):
        raise ValueError(f"no evidence source policy for consumer {consumer!r}")
    if not spec.get("workflows") or not spec.get("events"):
        raise ValueError(f"evidence source policy for {consumer!r} must list workflows and events")
    if spec.get("head_sha") not in {"exact", "ancestor"}:
        raise ValueError(f"evidence source policy for {consumer!r} must set head_sha to exact or ancestor")
    return spec


def validate_source_run(
    run: dict[str, Any],
    spec: dict[str, Any],
    *,
    repository: str,
    consumer_sha: str,
    is_ancestor: Callable[[str, str], bool],
) -> list[str]:
    """Returns the reasons the run is not an acceptable evidence source (empty when it is)."""
    issues: list[str] = []
    path = str(run.get("path") or "")
    if path not in spec["workflows"]:
        issues.append(f"run workflow {path or '<none>'} is not a registered producer: {spec['workflows']}")
    event = str(run.get("event") or "")
    if event not in spec["events"]:
        issues.append(f"run event {event or '<none>'} is not allowed: {spec['events']}")
    expected_branch = str(spec.get("head_branch") or "main")
    if run.get("head_branch") != expected_branch:
        issues.append(f"run head_branch must be {expected_branch}, got {run.get('head_branch')!r}")
    for field in ("repository", "head_repository"):
        full_name = str((run.get(field) or {}).get("full_name") or "")
        if full_name.casefold() != repository.casefold():
            issues.append(f"run {field} must be {repository}, got {full_name or '<none>'}")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        issues.append(f"run must have completed successfully, got status={run.get('status')!r} conclusion={run.get('conclusion')!r}")
    head_sha = str(run.get("head_sha") or "")
    if not SHA_RE.match(head_sha):
        issues.append("run head_sha is missing or malformed")
    elif spec["head_sha"] == "exact":
        if head_sha != consumer_sha:
            issues.append(f"run head_sha {head_sha} must equal the consuming commit {consumer_sha}")
    elif head_sha != consumer_sha and not is_ancestor(head_sha, consumer_sha):
        issues.append(f"run head_sha {head_sha} is not an ancestor of the consuming commit {consumer_sha}")
    return issues


def evidence_root(download_dir: Path) -> Path:
    nested = download_dir / "release" / "external-evidence"
    return nested if nested.is_dir() else download_dir


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, text=True, capture_output=True).stdout


def _gh_run(repository: str, run_id: str) -> dict[str, Any]:
    return json.loads(_gh("api", f"repos/{repository}/actions/runs/{run_id}"))


def _gh_is_ancestor(repository: str) -> Callable[[str, str], bool]:
    def is_ancestor(ancestor: str, descendant: str) -> bool:
        status = _gh("api", f"repos/{repository}/compare/{ancestor}...{descendant}", "--jq", ".status").strip()
        return status in {"ahead", "identical"}

    return is_ancestor


def _gh_download(repository: str, run_id: str, artifact_name: str, target: Path) -> None:
    _gh("run", "download", run_id, "--repo", repository, "--name", artifact_name, "--dir", str(target))


def fetch(
    *,
    consumer: str,
    run_id: str,
    artifact_name: str,
    dest: Path,
    repository: str,
    consumer_sha: str,
    policy: dict[str, Any],
    get_run: Callable[[str], dict[str, Any]],
    is_ancestor: Callable[[str, str], bool],
    download: Callable[[Path], None],
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "status": "failed",
        "consumer": consumer,
        "run_id": run_id,
        "artifact_name": artifact_name,
        "repository": repository,
        "consumer_sha": consumer_sha,
        "source_run": {},
        "copied_files": [],
        "ignored_files": [],
        "issues": [],
    }
    if not re.fullmatch(r"[0-9]+", run_id):
        report["issues"].append("run id must be numeric")
        return report
    if not ARTIFACT_NAME_RE.match(artifact_name):
        report["issues"].append("artifact name contains unsupported characters")
        return report
    if not SHA_RE.match(consumer_sha):
        report["issues"].append("consuming commit sha is missing or malformed")
        return report
    try:
        spec = consumer_policy(policy, consumer)
    except ValueError as exc:
        report["issues"].append(str(exc))
        return report

    run = get_run(run_id)
    report["source_run"] = {
        key: run.get(key)
        for key in ("id", "path", "event", "head_branch", "head_sha", "status", "conclusion", "html_url")
    }
    issues = validate_source_run(run, spec, repository=repository, consumer_sha=consumer_sha, is_ancestor=is_ancestor)
    if issues:
        report["issues"].extend(issues)
        return report

    quarantine = Path(tempfile.mkdtemp(prefix="evidence-download-", dir=os.environ.get("RUNNER_TEMP") or None))
    try:
        download(quarantine)
        result = import_external_ga_evidence.copy_expected_evidence(evidence_root(quarantine), dest)
    except ValueError as exc:
        report["issues"].append(str(exc))
        return report
    finally:
        shutil.rmtree(quarantine, ignore_errors=True)
    report["copied_files"] = result["copied"]
    report["ignored_files"] = result["ignored"]
    if not report["copied_files"]:
        report["issues"].append("the artifact contained none of the expected evidence files")
        return report
    report["status"] = "passed"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--consumer", required=True, help="Consumer key in release/evidence-source-workflows.json")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--artifact-name", required=True)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--consumer-sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--policy", default=str(POLICY_PATH))
    parser.add_argument("--write-report", default="")
    args = parser.parse_args(argv)

    if not args.repository:
        parser.error("--repository (or GITHUB_REPOSITORY) is required")
    report = fetch(
        consumer=args.consumer,
        run_id=args.run_id,
        artifact_name=args.artifact_name,
        dest=Path(args.dest),
        repository=args.repository,
        consumer_sha=args.consumer_sha,
        policy=load_policy(Path(args.policy)),
        get_run=lambda run_id: _gh_run(args.repository, run_id),
        is_ancestor=_gh_is_ancestor(args.repository),
        download=lambda target: _gh_download(args.repository, args.run_id, args.artifact_name, target),
    )
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.write_report:
        report_path = Path(args.write_report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(text, encoding="utf-8")
    print(text, end="")
    if report["status"] != "passed":
        for issue in report["issues"]:
            print(f"evidence source rejected: {issue}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
