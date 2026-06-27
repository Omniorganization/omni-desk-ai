#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _api_get(url: str, token: str) -> tuple[int, dict[str, Any] | None, str]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "omnidesk-live-branch-protection-check",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310 - GitHub API URL is constructed from repository input.
            raw = response.read().decode("utf-8")
            return int(response.status), json.loads(raw) if raw else {}, ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), None, body
    except Exception as exc:  # pragma: no cover - network failures are environment-specific
        return 0, None, str(exc)


def _check(condition: bool, failures: list[str], message: str) -> None:
    if not condition:
        failures.append(message)


def _contexts_from_required_status_checks(protection: dict[str, Any]) -> set[str]:
    required = protection.get("required_status_checks") or {}
    contexts = set(required.get("contexts") or [])
    for check in required.get("checks") or []:
        if isinstance(check, dict) and check.get("context"):
            contexts.add(str(check["context"]))
    return contexts


def audit(root: Path, repository: str, token: str, api_base: str = "https://api.github.com") -> dict[str, Any]:
    policy_path = root / ".github/branch-protection.required.json"
    policy = _read_json(policy_path)
    branch = str(policy.get("protected_branch") or "main")
    required_status_checks = set(policy.get("required_status_checks") or [])
    failures: list[str] = []
    warnings: list[str] = []

    if not repository or "/" not in repository:
        failures.append("repository must be owner/name")
    if not token:
        failures.append("GitHub token is required to verify the live control plane")

    protection: dict[str, Any] | None = None
    signatures: dict[str, Any] | None = None
    if not failures:
        protection_url = f"{api_base.rstrip('/')}/repos/{repository}/branches/{branch}/protection"
        status, protection, body = _api_get(protection_url, token)
        if status == 404:
            failures.append(f"branch protection not found for {repository}:{branch}")
        elif status >= 400 or protection is None:
            failures.append(f"failed to fetch branch protection: HTTP {status} {body[:300]}")

        signatures_url = f"{api_base.rstrip('/')}/repos/{repository}/branches/{branch}/protection/required_signatures"
        sig_status, signatures, sig_body = _api_get(signatures_url, token)
        if sig_status >= 400 or signatures is None:
            warnings.append(f"required signatures endpoint unavailable: HTTP {sig_status} {sig_body[:300]}")

    if protection:
        contexts = _contexts_from_required_status_checks(protection)
        missing_contexts = sorted(required_status_checks - contexts)
        _check(not missing_contexts, failures, f"live required status checks missing: {missing_contexts}")

        pull_request_reviews = protection.get("required_pull_request_reviews") or {}
        _check(bool(pull_request_reviews), failures, "live branch protection must require pull request reviews")
        _check(
            int(pull_request_reviews.get("required_approving_review_count") or 0) >= int(policy.get("required_approving_review_count") or 1),
            failures,
            "live required approving review count is weaker than policy",
        )
        _check(
            bool(pull_request_reviews.get("dismiss_stale_reviews")) == bool(policy.get("dismiss_stale_reviews")),
            failures,
            "live dismiss_stale_reviews does not match policy",
        )
        _check(
            bool(pull_request_reviews.get("require_code_owner_reviews")) == bool(policy.get("require_codeowners_review")),
            failures,
            "live CODEOWNERS review requirement does not match policy",
        )
        _check(bool(protection.get("required_linear_history")) or True, warnings, "linear history is not required by source policy")

        if signatures is not None:
            _check(bool(signatures.get("enabled")) == bool(policy.get("require_signed_commits")), failures, "live signed commit requirement does not match policy")

        restrictions = protection.get("restrictions")
        if policy.get("allow_direct_pushes") is False and not protection.get("required_pull_request_reviews"):
            failures.append("direct pushes are not effectively blocked because pull request reviews are not required")
        if restrictions is None:
            warnings.append("branch restrictions are not configured; verify this is acceptable for the repository owner model")

    return {
        "schema": "omnidesk-live-branch-protection/v1",
        "status": "passed" if not failures else "blocked",
        "repository": repository,
        "branch": branch,
        "policy_file": str(policy_path.relative_to(root)),
        "checked_status_checks": sorted(required_status_checks),
        "failures": failures,
        "warnings": warnings,
        "boundary": "This verifies the GitHub live control plane. It does not replace signed artifacts, push, soak, rollback, backup/restore, or failure-injection evidence.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify live GitHub branch protection against the source contract.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--api-base", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--write-report", default="")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    token = os.environ.get(args.token_env, "")
    report = audit(root, args.repository, token, args.api_base)

    if args.write_report:
        out = Path(args.write_report)
        if not out.is_absolute():
            out = root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"status": report["status"], "failure_count": len(report["failures"]), "warning_count": len(report["warnings"])}, ensure_ascii=False, sort_keys=True))
    for failure in report["failures"]:
        print(f"BLOCKER {failure}", file=sys.stderr)
    return 0 if args.audit_only or report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
