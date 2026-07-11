#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


GITHUB_API = "https://api.github.com"
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class LiveCheckResult:
    ok: bool
    failures: list[str]
    checks: list[str]
    report: dict[str, Any]


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def _api_get(url: str, token: str) -> tuple[int, Any, str]:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "omnidesk-live-branch-protection-check",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:  # nosec B310 - validated GitHub API URL.
            raw = response.read().decode("utf-8")
            return int(response.status), json.loads(raw) if raw else {}, ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), None, body
    except (OSError, urllib.error.URLError) as exc:  # pragma: no cover - network failures are environment-specific
        env = dict(os.environ)
        env["GH_TOKEN"] = token
        completed = subprocess.run(
            ["gh", "api", url],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        body = completed.stdout or completed.stderr
        try:
            payload = json.loads(body) if body else None
        except json.JSONDecodeError:
            payload = None
        if completed.returncode == 0:
            return 200, payload, ""
        status = 0
        if isinstance(payload, dict) and str(payload.get("status") or "").isdigit():
            status = int(payload["status"])
        return status, payload, f"urllib failed: {exc}; gh api failed: {body[:300]}"


def _check(condition: bool, failures: list[str], checks: list[str], failure: str, success: str) -> None:
    if condition:
        checks.append(success)
    else:
        failures.append(failure)


def _enabled(section: Any) -> bool:
    return isinstance(section, dict) and section.get("enabled") is True


def _legacy_contexts(protection: dict[str, Any]) -> set[str]:
    required = protection.get("required_status_checks") or {}
    contexts = {str(value) for value in required.get("contexts") or []}
    for check in required.get("checks") or []:
        if isinstance(check, dict) and check.get("context"):
            contexts.add(str(check["context"]))
    return contexts


def _rules_by_type(rules: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for rule in rules:
        rule_type = str(rule.get("type") or "")
        if rule_type:
            grouped.setdefault(rule_type, []).append(rule)
    return grouped


def _ruleset_contexts(grouped: dict[str, list[dict[str, Any]]]) -> set[str]:
    contexts: set[str] = set()
    for rule in grouped.get("required_status_checks", []):
        parameters = rule.get("parameters") or {}
        for check in parameters.get("required_status_checks") or []:
            if isinstance(check, dict) and check.get("context"):
                contexts.add(str(check["context"]))
    return contexts


def _first_parameters(grouped: dict[str, list[dict[str, Any]]], rule_type: str) -> dict[str, Any]:
    rules = grouped.get(rule_type) or []
    if not rules:
        return {}
    parameters = rules[0].get("parameters")
    return parameters if isinstance(parameters, dict) else {}


def _deployment_environments(grouped: dict[str, list[dict[str, Any]]]) -> set[str]:
    environments: set[str] = set()
    for rule in grouped.get("required_deployments", []):
        parameters = rule.get("parameters") or {}
        for value in parameters.get("required_deployment_environments") or []:
            environments.add(str(value))
    return environments


def _bypass_actor_key(actor: dict[str, Any]) -> str:
    actor_type = str(actor.get("actor_type") or "unknown")
    actor_id = str(actor.get("actor_id") or "unknown")
    bypass_mode = str(actor.get("bypass_mode") or "always")
    return f"{actor_type}:{actor_id}:{bypass_mode}"


def evaluate_live_protection(
    *,
    contract: dict[str, Any],
    repository: str,
    branch: str,
    protection: dict[str, Any] | None,
    signatures: dict[str, Any] | None,
    rules: list[dict[str, Any]] | None,
    rulesets: list[dict[str, Any]] | None,
    api_failures: list[str] | None = None,
) -> LiveCheckResult:
    failures = list(api_failures or [])
    checks: list[str] = []
    protection = protection or {}
    rules = rules or []
    rulesets = rulesets or []
    grouped = _rules_by_type(rules)

    applied_ruleset_ids = {
        int(rule["ruleset_id"])
        for rule in rules
        if isinstance(rule.get("ruleset_id"), int)
    }
    applied_rulesets = [
        item
        for item in rulesets
        if isinstance(item.get("id"), int) and int(item["id"]) in applied_ruleset_ids
    ]
    unresolved_rulesets = sorted(applied_ruleset_ids - {int(item["id"]) for item in applied_rulesets})
    if unresolved_rulesets:
        failures.append(f"cannot verify applied ruleset details: {unresolved_rulesets}")
    inactive_rulesets = sorted(
        int(item["id"])
        for item in applied_rulesets
        if str(item.get("enforcement") or "") != "active"
    )
    if inactive_rulesets:
        failures.append(f"applied rulesets are not active: {inactive_rulesets}")

    bypass_actor_keys = sorted(
        {
            _bypass_actor_key(actor)
            for ruleset in applied_rulesets
            for actor in (ruleset.get("bypass_actors") or [])
            if isinstance(actor, dict)
        }
    )
    allowed_bypass_actors = {str(value) for value in contract.get("allowed_bypass_actors") or []}
    unexpected_bypass_actors = sorted(set(bypass_actor_keys) - allowed_bypass_actors)
    _check(
        not unexpected_bypass_actors,
        failures,
        checks,
        f"unexpected ruleset bypass actors: {unexpected_bypass_actors}",
        "applied rulesets have no unauthorized bypass actors",
    )

    pull_parameters = _first_parameters(grouped, "pull_request")
    legacy_reviews = protection.get("required_pull_request_reviews") or {}
    pr_required = bool(pull_parameters) or bool(legacy_reviews)
    codeowner_required = bool(pull_parameters.get("require_code_owner_review")) or bool(
        legacy_reviews.get("require_code_owner_reviews")
    )
    stale_reviews_dismissed = bool(pull_parameters.get("dismiss_stale_reviews_on_push")) or bool(
        legacy_reviews.get("dismiss_stale_reviews")
    )
    conversation_resolution = bool(pull_parameters.get("required_review_thread_resolution")) or _enabled(
        protection.get("required_conversation_resolution")
    )
    last_push_approval = bool(pull_parameters.get("require_last_push_approval"))
    approving_review_count = max(
        int(pull_parameters.get("required_approving_review_count") or 0),
        int(legacy_reviews.get("required_approving_review_count") or 0),
    )

    status_parameters = _first_parameters(grouped, "required_status_checks")
    contexts = _legacy_contexts(protection) | _ruleset_contexts(grouped)
    strict_status_checks = bool(status_parameters.get("strict_required_status_checks_policy")) or bool(
        (protection.get("required_status_checks") or {}).get("strict")
    )
    required_contexts = {
        str(value)
        for value in (contract.get("required_check_contexts") or contract.get("required_status_checks") or [])
    }
    missing_contexts = sorted(required_contexts - contexts)

    signed_commits = "required_signatures" in grouped or _enabled(signatures) or _enabled(
        protection.get("required_signatures")
    )
    linear_history = "required_linear_history" in grouped or _enabled(protection.get("required_linear_history"))
    force_pushes_blocked = "non_fast_forward" in grouped or (
        bool(protection) and not _enabled(protection.get("allow_force_pushes"))
    )
    deletions_blocked = "deletion" in grouped or (
        bool(protection) and not _enabled(protection.get("allow_deletions"))
    )
    lock_branch = "lock_branch" in grouped or _enabled(protection.get("lock_branch"))
    deployments = _deployment_environments(grouped)
    admins_enforced = _enabled(protection.get("enforce_admins")) or (
        bool(applied_rulesets) and not bypass_actor_keys and not inactive_rulesets
    )
    direct_pushes_blocked = pr_required and admins_enforced

    _check(not missing_contexts, failures, checks, f"live required status checks missing: {missing_contexts}", "required live status checks match")
    _check(
        strict_status_checks == bool(contract.get("require_branch_up_to_date")),
        failures,
        checks,
        "live strict/up-to-date status-check policy does not match the source contract",
        "strict up-to-date status checks match",
    )
    _check(pr_required == bool(contract.get("require_pull_request")), failures, checks, "live pull-request requirement does not match", "pull-request requirement matches")
    _check(codeowner_required == bool(contract.get("require_codeowners_review")), failures, checks, "live Code Owner review requirement does not match", "Code Owner review requirement matches")
    _check(stale_reviews_dismissed == bool(contract.get("dismiss_stale_reviews")), failures, checks, "live stale-review dismissal does not match", "stale-review dismissal matches")
    _check(conversation_resolution == bool(contract.get("require_conversation_resolution")), failures, checks, "live conversation resolution does not match", "conversation resolution matches")
    _check(
        approving_review_count >= int(contract.get("required_approving_review_count") or 0),
        failures,
        checks,
        "live approving-review count is weaker than the source contract",
        "approving-review count matches",
    )
    if contract.get("require_last_push_approval") is not None:
        _check(last_push_approval == bool(contract.get("require_last_push_approval")), failures, checks, "live last-push approval requirement does not match", "last-push approval matches")
    _check(signed_commits == bool(contract.get("require_signed_commits")), failures, checks, "live signed-commit requirement does not match", "signed-commit requirement matches")
    _check(linear_history == bool(contract.get("require_linear_history")), failures, checks, "live linear-history requirement does not match", "linear-history requirement matches")
    _check(admins_enforced == bool(contract.get("require_enforce_admins")), failures, checks, "live admin enforcement does not match", "admin enforcement matches")
    _check(force_pushes_blocked == (contract.get("allow_force_pushes") is False), failures, checks, "live force-push policy does not match", "force pushes are blocked")
    _check(deletions_blocked == (contract.get("allow_deletions") is False), failures, checks, "live deletion policy does not match", "branch deletion is blocked")
    _check(direct_pushes_blocked == (contract.get("allow_direct_pushes") is False), failures, checks, "live direct-push policy does not match", "direct pushes are blocked for administrators")
    if contract.get("require_lock_branch") is not None:
        _check(lock_branch == bool(contract.get("require_lock_branch")), failures, checks, "live lock-branch setting does not match", "lock-branch setting matches")
    required_deployments = {str(value) for value in contract.get("required_deployments") or []}
    _check(required_deployments.issubset(deployments), failures, checks, f"live required deployment environments missing: {sorted(required_deployments - deployments)}", "required deployment environments match")

    effective = {
        "required_status_checks": sorted(contexts),
        "strict_required_status_checks": strict_status_checks,
        "pull_request_required": pr_required,
        "code_owner_review_required": codeowner_required,
        "dismiss_stale_reviews": stale_reviews_dismissed,
        "conversation_resolution_required": conversation_resolution,
        "last_push_approval_required": last_push_approval,
        "required_approving_review_count": approving_review_count,
        "signed_commits_required": signed_commits,
        "linear_history_required": linear_history,
        "admins_enforced": admins_enforced,
        "direct_pushes_blocked": direct_pushes_blocked,
        "force_pushes_blocked": force_pushes_blocked,
        "deletions_blocked": deletions_blocked,
        "lock_branch": lock_branch,
        "required_deployments": sorted(deployments),
        "bypass_actors": bypass_actor_keys,
        "applied_ruleset_ids": sorted(applied_ruleset_ids),
    }
    report = {
        "schema": "omnidesk-live-branch-protection/v3",
        "status": "passed" if not failures else "blocked",
        "produced_at": datetime.now(timezone.utc).isoformat(),
        "producer": "OmniDesk live GitHub branch protection checker",
        "repository": repository,
        "branch": branch,
        "effective_protection": effective,
        "required_status_checks": sorted(required_contexts),
        "checked_status_checks": sorted(contexts),
        "ruleset_ids": sorted(applied_ruleset_ids),
        "failures": failures,
        "checks": checks,
        "boundary": "This verifies effective legacy branch protection and active Rulesets; it does not replace release, runtime, or operations evidence.",
    }
    return LiveCheckResult(not failures, failures, checks, report)


def audit(
    root: Path,
    repository: str,
    token: str,
    api_base: str = GITHUB_API,
    *,
    api_get: Callable[[str, str], tuple[int, Any, str]] = _api_get,
) -> dict[str, Any]:
    policy_path = root / ".github/branch-protection.required.json"
    policy = _read_json(policy_path)
    branch = str(policy.get("protected_branch") or policy.get("base_branch") or "main")
    failures: list[str] = []
    if not REPOSITORY_RE.fullmatch(repository):
        failures.append("repository must be owner/name using GitHub-safe characters")
    if not token:
        failures.append("GitHub token is required to verify the live control plane")
    if failures:
        return evaluate_live_protection(
            contract=policy,
            repository=repository,
            branch=branch,
            protection=None,
            signatures=None,
            rules=None,
            rulesets=None,
            api_failures=failures,
        ).report

    base = api_base.rstrip("/")
    protection_status, protection_value, protection_body = api_get(
        f"{base}/repos/{repository}/branches/{branch}/protection", token
    )
    signature_status, signature_value, signature_body = api_get(
        f"{base}/repos/{repository}/branches/{branch}/protection/required_signatures", token
    )
    rules_status, rules_value, rules_body = api_get(
        f"{base}/repos/{repository}/rules/branches/{branch}", token
    )
    protection = protection_value if protection_status == 200 and isinstance(protection_value, dict) else None
    signatures = signature_value if signature_status == 200 and isinstance(signature_value, dict) else None
    rules = rules_value if rules_status == 200 and isinstance(rules_value, list) else None
    api_failures: list[str] = []
    if protection is None and rules is None:
        api_failures.append(
            f"cannot read either branch protection or applied branch rules: protection HTTP {protection_status} {protection_body[:200]}; rules HTTP {rules_status} {rules_body[:200]}"
        )
    applied_ids = {
        int(rule["ruleset_id"])
        for rule in (rules or [])
        if isinstance(rule, dict) and isinstance(rule.get("ruleset_id"), int)
    }
    rulesets: list[dict[str, Any]] = []
    for ruleset_id in sorted(applied_ids):
        status, value, body = api_get(f"{base}/repos/{repository}/rulesets/{ruleset_id}", token)
        if status == 200 and isinstance(value, dict):
            rulesets.append(value)
        else:
            api_failures.append(f"cannot read applied ruleset {ruleset_id}: HTTP {status} {body[:200]}")
    if signature_status not in {200, 404} and "required_signatures" not in _rules_by_type(rules or []):
        api_failures.append(f"cannot verify required signatures: HTTP {signature_status} {signature_body[:200]}")

    result = evaluate_live_protection(
        contract=policy,
        repository=repository,
        branch=branch,
        protection=protection,
        signatures=signatures,
        rules=rules,
        rulesets=rulesets,
        api_failures=api_failures,
    )
    result.report["policy_file"] = str(policy_path.relative_to(root))
    return result.report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify effective live GitHub branch protection and Rulesets.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--api-base", default=os.environ.get("GITHUB_API_URL", GITHUB_API))
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--write-report", default="")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    report = audit(root, args.repository, os.environ.get(args.token_env, ""), args.api_base)
    if args.write_report:
        out = Path(args.write_report)
        if not out.is_absolute():
            out = root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "failure_count": len(report["failures"]),
                "check_count": len(report.get("checks") or []),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    for failure in report["failures"]:
        print(f"BLOCKER {failure}", file=sys.stderr)
    return 0 if args.audit_only or report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
