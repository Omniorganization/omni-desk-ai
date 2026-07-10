#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


API_VERSION = "2022-11-28"


def _api_get(url: str, token: str) -> tuple[int, dict[str, Any] | None, str]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "omnidesk-main-verification-artifact-check",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310 - GitHub API URL is constructed from workflow inputs.
            raw = response.read().decode("utf-8")
            return int(response.status), json.loads(raw) if raw else {}, ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), None, body
    except Exception as exc:  # pragma: no cover - network failures are environment-specific
        return 0, None, str(exc)


def audit(repository: str, commit_sha: str, token: str, api_base: str = "https://api.github.com") -> dict[str, Any]:
    failures: list[str] = []
    artifact_name = f"main-verification-evidence-{commit_sha}"
    if not repository or "/" not in repository:
        failures.append("repository must be owner/name")
    if not commit_sha:
        failures.append("commit sha is required")
    if not token:
        failures.append("GitHub token is required")
    if failures:
        return {"schema": "omnidesk-main-verification-live-artifact/v1", "status": "blocked", "failures": failures}

    # Only an explicit workflow_dispatch can bind an external evidence run id. PR and push
    # runs intentionally verify source readiness while emitting a blocked customer-GA status.
    runs_url = f"{api_base.rstrip('/')}/repos/{repository}/actions/runs?head_sha={commit_sha}&event=workflow_dispatch&per_page=50"
    status, runs_doc, body = _api_get(runs_url, token)
    if status >= 400 or runs_doc is None:
        return {
            "schema": "omnidesk-main-verification-live-artifact/v1",
            "status": "blocked",
            "commit": commit_sha,
            "artifact_name": artifact_name,
            "failures": [f"failed to fetch workflow runs: HTTP {status} {body[:300]}"],
        }

    matching_runs = [
        run
        for run in runs_doc.get("workflow_runs", [])
        if run.get("head_sha") == commit_sha
        and str(run.get("name") or "") == "Main Verification"
        and str(run.get("event") or "") == "workflow_dispatch"
        and str(run.get("conclusion") or "") == "success"
    ]
    if not matching_runs:
        failures.append("no successful enforced Main Verification workflow_dispatch run found for commit")

    artifacts: list[dict[str, Any]] = []
    for run in matching_runs:
        artifacts_url = f"{api_base.rstrip('/')}/repos/{repository}/actions/runs/{run['id']}/artifacts?name={artifact_name}"
        artifact_status, artifacts_doc, artifact_body = _api_get(artifacts_url, token)
        if artifact_status >= 400 or artifacts_doc is None:
            failures.append(f"failed to fetch artifacts for run {run['id']}: HTTP {artifact_status} {artifact_body[:300]}")
            continue
        artifacts.extend(artifacts_doc.get("artifacts", []))

    live_artifacts = [artifact for artifact in artifacts if artifact.get("name") == artifact_name and not artifact.get("expired")]
    if not live_artifacts:
        failures.append(f"missing unexpired artifact: {artifact_name}")

    return {
        "schema": "omnidesk-main-verification-live-artifact/v1",
        "status": "passed" if not failures else "blocked",
        "repository": repository,
        "commit": commit_sha,
        "artifact_name": artifact_name,
        "required_event": "workflow_dispatch",
        "matching_run_ids": [run.get("id") for run in matching_runs],
        "artifact_ids": [artifact.get("id") for artifact in live_artifacts],
        "failures": failures,
        "policy": (
            "Real GA requires a successful Main Verification workflow_dispatch for the exact commit. "
            "That dispatch requires an external evidence run id and fails unless the "
            "complete semantic Real GA audit and native/signed binding both pass."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify live enforced Main Verification artifact for a commit.")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--commit", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--api-base", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--write-report", default="")
    args = parser.parse_args(argv)
    token = os.environ.get(args.token_env, "")
    report = audit(args.repository, args.commit, token, args.api_base)
    if args.write_report:
        from pathlib import Path

        out = Path(args.write_report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "failure_count": len(report.get("failures", []))}, ensure_ascii=False, sort_keys=True))
    for failure in report.get("failures", []):
        print(f"BLOCKER {failure}", file=sys.stderr)
    return 0 if args.audit_only or report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
