#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REQUIRED_STATUS_CHECKS = {
    "Release Policy",
    "CI",
    "Security",
    "Security Attack Surface Gate",
    "Docker Image Scan",
    "Tri-App Quality Gate",
    "Main Verification",
    "Team Governance",
    "Self Upgrade Gate",
    "Source Maturity Closure",
}

REQUIRED_JOBS = {
    "release-policy",
    "external-ga-evidence-contract",
    "test (3.10)",
    "test (3.11)",
    "test (3.12)",
    "test (3.13)",
    "web-admin",
    "desktop-tauri",
    "mobile-flutter",
    "mobile-ios-simulator",
    "backend-and-contract",
    "main-verification",
    "team-governance",
    "security-attack-surface",
    "source-maturity-closure",
}

REQUIRED_CHECK_CONTEXTS = {
    "CI",
    "Security",
    "release-policy",
    "external-ga-evidence-contract",
    "security-attack-surface",
    "docker-scan",
    "backend-and-contract",
    "web-admin",
    "desktop-tauri",
    "mobile-flutter",
    "mobile-ios-simulator",
    "main-verification",
    "team-governance",
    "source-maturity-closure",
    "feasible-closure",
    "remaining-real-evidence-source-fixes",
}

REQUIRED_BOOLEAN_TRUE = (
    "require_pull_request",
    "require_branch_up_to_date",
    "require_conversation_resolution",
    "require_codeowners_review",
    "dismiss_stale_reviews",
    "require_signed_commits",
    "require_enforce_admins",
    "require_linear_history",
)

REQUIRED_BOOLEAN_FALSE = (
    "allow_direct_pushes",
    "allow_deletions",
)


def _load_json(path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"missing branch protection contract: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _check(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate repository branch protection source contract.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    contract = _load_json(root / ".github" / "branch-protection.required.json")
    failures: list[str] = []

    _check(contract.get("schema") == "omnidesk-branch-protection/v1", "branch protection schema must be omnidesk-branch-protection/v1", failures)
    _check(contract.get("protected_branch") == "main", "protected branch must be main", failures)
    for key in REQUIRED_BOOLEAN_TRUE:
        _check(contract.get(key) is True, f"{key} must be true", failures)
    for key in REQUIRED_BOOLEAN_FALSE:
        _check(contract.get(key) is False, f"{key} must be false", failures)
    _check(int(contract.get("required_approving_review_count", 0)) >= 1, "at least one approving review is required", failures)

    status_checks = set(contract.get("required_status_checks") or [])
    _check(REQUIRED_STATUS_CHECKS.issubset(status_checks), f"required status checks missing: {sorted(REQUIRED_STATUS_CHECKS - status_checks)}", failures)
    check_contexts = set(contract.get("required_check_contexts") or [])
    _check(
        REQUIRED_CHECK_CONTEXTS.issubset(check_contexts),
        f"required check contexts missing: {sorted(REQUIRED_CHECK_CONTEXTS - check_contexts)}",
        failures,
    )
    jobs = set(contract.get("required_jobs") or [])
    _check(REQUIRED_JOBS.issubset(jobs), f"required jobs missing: {sorted(REQUIRED_JOBS - jobs)}", failures)

    merge_policy = contract.get("merge_policy") or {}
    _check(merge_policy.get("require_all_required_checks_success") is True, "merge policy must require successful required checks", failures)
    _check(merge_policy.get("block_pending_required_checks") is True, "merge policy must block pending required checks", failures)
    _check(merge_policy.get("require_branch_up_to_date") is True, "merge policy must require up-to-date branches", failures)
    _check(merge_policy.get("require_enforce_admins") is True, "merge policy must enforce branch protection for admins", failures)
    _check(merge_policy.get("require_linear_history") is True, "merge policy must require linear history", failures)

    team_policy = contract.get("team_governance") or {}
    _check(team_policy.get("required_for_customer_distribution_ga") is True, "team governance must be required for customer-distribution GA", failures)
    _check(team_policy.get("source_contract") == ".github/team-governance.required.json", "team governance source contract must be bound", failures)
    _check(team_policy.get("live_evidence") == "release/external-evidence/control-plane/github-team-governance-live.json", "team governance live evidence path must be bound", failures)
    _check(team_policy.get("require_organization_owner") is True, "team governance must require organization owner", failures)
    _check(team_policy.get("require_resolved_github_teams") is True, "team governance must require resolved GitHub teams", failures)
    _check(team_policy.get("forbid_personal_owner_fallback_for_real_ga") is True, "team governance must forbid personal owner fallback for Real GA", failures)

    distribution_policy = contract.get("distribution_ga_policy") or {}
    _check(distribution_policy.get("require_main_verification_artifact") is True, "distribution GA must require main verification artifact", failures)
    _check(distribution_policy.get("require_main_verification_live_artifact") is True, "distribution GA must require live main verification artifact", failures)
    _check(distribution_policy.get("require_team_codeowners") is True, "distribution GA must require team CODEOWNERS", failures)
    _check(distribution_policy.get("require_external_ga_evidence_passed") is True, "distribution GA must require external evidence to pass", failures)
    _check(distribution_policy.get("forbid_mock_or_sample_evidence") is True, "distribution GA must forbid mock/sample evidence", failures)

    if failures:
        print("branch protection contract check failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("branch protection contract ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
