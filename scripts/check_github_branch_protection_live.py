#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request


GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class LiveCheckResult:
    ok: bool
    failures: list[str]
    checks: list[str]
    report: dict[str, Any]


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _repo_from_remote(root: Path) -> str | None:
    completed = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        return None
    remote = completed.stdout.strip()
    patterns = (
        r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$",
        r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.search(pattern, remote)
        if match:
            return f"{match.group('owner')}/{match.group('repo')}"
    return None


def _token() -> str | None:
    return os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")


def _gh_api_get_json(repo: str, branch: str) -> tuple[int, dict[str, Any]]:
    completed = subprocess.run(
        ["gh", "api", f"repos/{repo}/branches/{branch}/protection"],
        text=True,
        capture_output=True,
        check=False,
    )
    body = completed.stdout or completed.stderr
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        payload = {"message": body.strip() or f"gh api exited {completed.returncode}"}
    if completed.returncode == 0:
        return 200, payload
    status = int(payload.get("status") or 0) if str(payload.get("status") or "").isdigit() else 0
    if status:
        return status, payload
    message = str(payload.get("message") or "")
    if "Branch not protected" in message:
        return 404, payload
    return 0, payload


def _api_get_json(url: str, token: str | None, *, repo: str, branch: str) -> tuple[int, dict[str, Any]]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "omnidesk-branch-protection-live-check",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = request.Request(url, headers=headers)
    try:
        with request.urlopen(req, timeout=20) as response:  # noqa: S310 - fixed GitHub API URL.
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {"message": body}
        return int(exc.code), payload
    except (OSError, error.URLError) as exc:
        status, payload = _gh_api_get_json(repo, branch)
        if status:
            return status, payload
        return 0, {"message": f"urllib failed and gh fallback failed: {exc}; {payload.get('message', '')}"}


def _status_contexts(protection: dict[str, Any]) -> set[str]:
    checks = protection.get("required_status_checks") or {}
    contexts = set(checks.get("contexts") or [])
    for item in checks.get("checks") or []:
        context = item.get("context")
        if context:
            contexts.add(str(context))
    return contexts


def _enabled(section: Any) -> bool:
    return bool(section and section.get("enabled") is True)


def evaluate_live_protection(
    *,
    contract: dict[str, Any],
    protection_status: int,
    protection: dict[str, Any],
    repo: str,
    branch: str,
) -> LiveCheckResult:
    failures: list[str] = []
    checks: list[str] = []

    if protection_status == 404:
        failures.append(f"live branch protection is not enabled for {repo}:{branch}")
        report = {
            "schema_version": "omnidesk-github-branch-protection-live/v1",
            "ok": False,
            "repo": repo,
            "branch": branch,
            "status": "not_protected",
            "failures": failures,
            "checks": checks,
        }
        return LiveCheckResult(False, failures, checks, report)
    if protection_status != 200:
        message = protection.get("message") or f"GitHub API returned HTTP {protection_status}"
        failures.append(f"cannot read live branch protection for {repo}:{branch}: {message}")
        report = {
            "schema_version": "omnidesk-github-branch-protection-live/v1",
            "ok": False,
            "repo": repo,
            "branch": branch,
            "status": "unreadable",
            "failures": failures,
            "checks": checks,
        }
        return LiveCheckResult(False, failures, checks, report)

    required_contexts = {
        str(item)
        for item in (
            contract.get("required_check_contexts")
            or contract.get("required_status_checks", [])
        )
    }
    actual_contexts = _status_contexts(protection)
    missing_contexts = sorted(required_contexts - actual_contexts)
    if missing_contexts:
        failures.append(f"missing required live status checks: {', '.join(missing_contexts)}")
    else:
        checks.append("required live status checks match the source contract")

    pr_reviews = protection.get("required_pull_request_reviews") or {}
    if contract.get("require_pull_request") and not pr_reviews:
        failures.append("required pull request reviews are not enabled")
    else:
        checks.append("pull request review protection is enabled")

    if contract.get("require_codeowners_review") and not pr_reviews.get("require_code_owner_reviews"):
        failures.append("CODEOWNERS review is not required")
    else:
        checks.append("CODEOWNERS review requirement matches")

    expected_count = int(contract.get("required_approving_review_count") or 0)
    actual_count = int(pr_reviews.get("required_approving_review_count") or 0)
    if actual_count < expected_count:
        failures.append(f"approving review count is {actual_count}, expected at least {expected_count}")
    else:
        checks.append("approving review count matches")

    if contract.get("dismiss_stale_reviews") and not pr_reviews.get("dismiss_stale_reviews"):
        failures.append("stale review dismissal is not enabled")
    else:
        checks.append("stale review dismissal matches")

    if contract.get("require_conversation_resolution") and not _enabled(protection.get("required_conversation_resolution")):
        failures.append("conversation resolution is not required")
    else:
        checks.append("conversation resolution requirement matches")

    if contract.get("allow_force_pushes") is False and _enabled(protection.get("allow_force_pushes")):
        failures.append("force pushes are allowed")
    else:
        checks.append("force pushes are disabled")

    if contract.get("allow_deletions") is False and _enabled(protection.get("allow_deletions")):
        failures.append("branch deletion is allowed")
    else:
        checks.append("branch deletion is disabled")

    if contract.get("allow_direct_pushes") is False and not _enabled(protection.get("enforce_admins")):
        failures.append("branch protection is not enforced for administrators")
    else:
        checks.append("branch protection is enforced for administrators")

    ok = not failures
    report = {
        "schema_version": "omnidesk-github-branch-protection-live/v1",
        "ok": ok,
        "repo": repo,
        "branch": branch,
        "status": "passed" if ok else "failed",
        "required_status_checks": sorted(required_contexts),
        "live_status_checks": sorted(actual_contexts),
        "failures": failures,
        "checks": checks,
    }
    return LiveCheckResult(ok, failures, checks, report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare live GitHub branch protection against .github/branch-protection.required.json."
    )
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--repo", help="GitHub repository in owner/name form. Defaults to GITHUB_REPOSITORY or origin.")
    parser.add_argument("--branch", help="Branch to inspect. Defaults to the contract base_branch.")
    parser.add_argument("--write-report", help="Optional path for a machine-readable JSON report.")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    contract = _read_json(root / ".github" / "branch-protection.required.json")
    repo = args.repo or os.getenv("GITHUB_REPOSITORY") or _repo_from_remote(root)
    branch = args.branch or str(contract.get("base_branch") or "main")
    if not repo:
        print("BLOCKER cannot infer GitHub repository; pass --repo owner/name", file=sys.stderr)
        return 1

    status, payload = _api_get_json(
        f"{GITHUB_API}/repos/{repo}/branches/{branch}/protection",
        _token(),
        repo=repo,
        branch=branch,
    )
    result = evaluate_live_protection(
        contract=contract,
        protection_status=status,
        protection=payload,
        repo=repo,
        branch=branch,
    )

    if args.write_report:
        Path(args.write_report).write_text(json.dumps(result.report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    for message in result.checks:
        print(f"OK      {message}")
    for message in result.failures:
        print(f"BLOCKER {message}", file=sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
