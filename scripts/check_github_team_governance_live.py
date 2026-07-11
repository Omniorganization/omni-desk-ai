#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from scripts.check_live_branch_protection_contract import GITHUB_API, _api_get, audit as audit_branch_protection
except ModuleNotFoundError:  # Direct execution sets scripts/ as sys.path[0].
    from check_live_branch_protection_contract import GITHUB_API, _api_get, audit as audit_branch_protection


REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
PERMISSION_RANK = {"pull": 1, "triage": 2, "push": 3, "maintain": 4, "admin": 5}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def _git_head(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _team_slug(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("slug") or item.get("name") or "").strip()
    return ""


def _member_login(item: Any) -> str:
    return str(item.get("login") or "").strip() if isinstance(item, dict) else ""


def _normalize_codeowners_pattern(value: str) -> str:
    pattern = value.strip()
    return pattern if pattern == "*" else pattern.lstrip("/")


def _codeowners_rules(path: Path) -> dict[str, set[str]]:
    rules: dict[str, set[str]] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = line.split()
        if len(tokens) >= 2:
            rules[_normalize_codeowners_pattern(tokens[0])] = set(tokens[1:])
    return rules


def _api_failure(label: str, status: int, body: str) -> str:
    summary = " ".join(str(body or "").split())[:240]
    return f"cannot read {label}: HTTP {status} {summary}".rstrip()


def evaluate_team_governance(
    *,
    root: Path,
    contract: dict[str, Any],
    repository: str,
    commit: str,
    repository_doc: dict[str, Any] | None,
    repository_teams: list[dict[str, Any]] | None,
    commit_doc: dict[str, Any] | None,
    members_by_team: dict[str, list[dict[str, Any]]],
    branch_report: dict[str, Any] | None,
    api_failures: list[str] | None = None,
) -> dict[str, Any]:
    failures = list(api_failures or [])
    checks: list[str] = []
    organization = str(contract.get("required_organization") or "").strip()
    codeowners_rel = str(contract.get("codeowners_file") or ".github/CODEOWNERS")
    codeowners_path = root / codeowners_rel
    required_specs = {
        _team_slug(item): item
        for item in contract.get("required_teams") or []
        if _team_slug(item)
    }
    min_members = int(contract.get("minimum_members_per_team") or 0)
    min_independent = int(contract.get("minimum_independent_reviewers_per_team") or 0)

    owner = repository_doc.get("owner") if isinstance(repository_doc, dict) else None
    owner_login = str(owner.get("login") or "").strip() if isinstance(owner, dict) else ""
    owner_type = str(owner.get("type") or "").strip() if isinstance(owner, dict) else ""
    repository_is_organization_owned = owner_type == "Organization" and owner_login.lower() == organization.lower()
    if repository_is_organization_owned:
        checks.append("repository is owned by the required GitHub organization")
    else:
        failures.append("repository owner must be the required GitHub Organization")

    author_doc = commit_doc.get("author") if isinstance(commit_doc, dict) else None
    commit_author = _member_login(author_doc)
    if not commit_author:
        failures.append("checked commit must resolve to a GitHub author for reviewer-independence verification")
    else:
        checks.append("checked commit author resolved")

    owners: set[str] = set()
    codeowners_rules: dict[str, set[str]] = {}
    if not codeowners_path.is_file():
        failures.append(f"CODEOWNERS file is missing: {codeowners_rel}")
    else:
        codeowners_rules = _codeowners_rules(codeowners_path)
        owners = {owner for rule_owners in codeowners_rules.values() for owner in rule_owners}
    expected_team_owners = {f"@{organization}/{slug}" for slug in required_specs}
    missing_team_owners = sorted(expected_team_owners - owners)
    unexpected_owners = sorted(owners - expected_team_owners)
    codeowners_team_owned = bool(owners) and not missing_team_owners and not unexpected_owners
    if missing_team_owners:
        failures.append(f"CODEOWNERS is missing required teams: {missing_team_owners}")
    if unexpected_owners:
        failures.append(f"CODEOWNERS contains personal or unauthorized owners: {unexpected_owners}")
    missing_path_ownership: list[str] = []
    for slug, spec in required_specs.items():
        expected_owner = f"@{organization}/{slug}"
        for required_path in spec.get("required_paths") or []:
            pattern = _normalize_codeowners_pattern(str(required_path))
            if expected_owner not in codeowners_rules.get(pattern, set()):
                missing_path_ownership.append(f"{pattern}:{expected_owner}")
    if missing_path_ownership:
        failures.append(f"CODEOWNERS required path ownership is missing: {sorted(missing_path_ownership)}")
        codeowners_team_owned = False
    if codeowners_team_owned:
        checks.append("CODEOWNERS resolves exclusively to the required organization teams")

    live_teams = {
        _team_slug(item): item
        for item in (repository_teams or [])
        if isinstance(item, dict) and _team_slug(item)
    }
    team_results: list[dict[str, Any]] = []
    all_teams_resolved = True
    separation_satisfied = True
    for slug, spec in required_specs.items():
        team = live_teams.get(slug)
        members = members_by_team.get(slug) or []
        member_logins = sorted({login for login in (_member_login(item) for item in members) if login})
        independent = sorted(login for login in member_logins if login.lower() != commit_author.lower())
        required_permission = str(spec.get("minimum_repository_permission") or "push").strip().lower()
        actual_permission = str((team or {}).get("permission") or "").strip().lower()
        privacy = str((team or {}).get("privacy") or "").strip().lower()
        team_exists = team is not None
        permission_ok = PERMISSION_RANK.get(actual_permission, 0) >= PERMISSION_RANK.get(required_permission, 99)
        visible = privacy in {"closed", "visible"}
        member_count_ok = len(member_logins) >= min_members
        independent_ok = bool(commit_author) and len(independent) >= min_independent
        resolved = team_exists and permission_ok and visible
        valid = resolved and member_count_ok and independent_ok
        all_teams_resolved = all_teams_resolved and resolved
        separation_satisfied = separation_satisfied and member_count_ok and independent_ok
        if not team_exists:
            failures.append(f"required repository team is missing: {slug}")
        elif not visible:
            failures.append(f"team {slug} must be closed/visible for CODEOWNERS resolution")
        if team_exists and not permission_ok:
            failures.append(
                f"team {slug} repository permission {actual_permission or 'none'} is weaker than {required_permission}"
            )
        if not member_count_ok:
            failures.append(f"team {slug} has {len(member_logins)} members; at least {min_members} are required")
        if not independent_ok:
            failures.append(
                f"team {slug} has {len(independent)} reviewer(s) independent of commit author {commit_author or 'unknown'}; at least {min_independent} are required"
            )
        team_results.append(
            {
                "slug": slug,
                "privacy": privacy or None,
                "repository_permission": actual_permission or None,
                "minimum_repository_permission": required_permission,
                "member_count": len(member_logins),
                "minimum_member_count": min_members,
                "members": member_logins,
                "independent_reviewers": independent,
                "minimum_independent_reviewers": min_independent,
                "resolved": resolved,
                "valid": valid,
            }
        )
    if all_teams_resolved:
        checks.append("all required teams are visible and have explicit repository write-or-higher permission")
    if separation_satisfied:
        checks.append("all required teams satisfy independent-reviewer separation")

    branch_effective = branch_report.get("effective_protection") if isinstance(branch_report, dict) else None
    branch_effective = branch_effective if isinstance(branch_effective, dict) else {}
    branch_passed = isinstance(branch_report, dict) and branch_report.get("status") == "passed"
    codeowner_review = branch_passed and branch_effective.get("code_owner_review_required") is True
    admins_enforced = branch_passed and branch_effective.get("admins_enforced") is True
    bypass_absent = branch_passed and not (branch_effective.get("bypass_actors") or [])
    if not branch_passed:
        failures.append("effective live branch-protection report must pass")
    if not codeowner_review:
        failures.append("effective branch protection must require Code Owner review")
    if not admins_enforced:
        failures.append("effective branch protection must enforce administrators")
    if not bypass_absent:
        failures.append("effective branch Rulesets must not contain bypass actors")

    report = {
        "schema": "omnidesk-team-governance-live/v1",
        "status": "passed" if not failures else "blocked",
        "produced_at": datetime.now(timezone.utc).isoformat(),
        "producer": "OmniDesk live GitHub team governance checker",
        "repository": repository,
        "owner": owner_login or None,
        "owner_type": owner_type or None,
        "organization": organization,
        "codeowners_ref": commit,
        "checked_commit_author": commit_author or None,
        "repository_is_organization_owned": repository_is_organization_owned,
        "required_teams_resolved": all_teams_resolved,
        "required_team_member_separation_satisfied": separation_satisfied,
        "codeowners_team_owned": codeowners_team_owned,
        "branch_protection_requires_codeowners_review": codeowner_review,
        "admins_enforced": admins_enforced,
        "ruleset_bypass_actors_absent": bypass_absent,
        "personal_owner_fallback_active": bool(unexpected_owners),
        "required_teams": team_results,
        "codeowners_owners": sorted(owners),
        "codeowners_rules": {pattern: sorted(rule_owners) for pattern, rule_owners in codeowners_rules.items()},
        "failures": failures,
        "checks": checks,
        "boundary": "This verifies live organization ownership, team access, membership separation, CODEOWNERS resolution, and effective branch enforcement for the checked commit.",
    }
    return report


def audit(
    root: Path,
    repository: str,
    commit: str,
    token: str,
    api_base: str = GITHUB_API,
    *,
    branch_report: dict[str, Any] | None = None,
    api_get: Callable[[str, str], tuple[int, Any, str]] = _api_get,
) -> dict[str, Any]:
    contract = _read_json(root / ".github/team-governance.required.json")
    failures: list[str] = []
    if not REPOSITORY_RE.fullmatch(repository):
        failures.append("repository must be owner/name using GitHub-safe characters")
    if not COMMIT_RE.fullmatch(commit):
        failures.append("commit must be a 7-64 character hexadecimal Git commit id")
    if not token:
        failures.append("GitHub token is required to verify live team governance")
    if failures:
        return evaluate_team_governance(
            root=root,
            contract=contract,
            repository=repository,
            commit=commit,
            repository_doc=None,
            repository_teams=None,
            commit_doc=None,
            members_by_team={},
            branch_report=branch_report,
            api_failures=failures,
        )

    base = api_base.rstrip("/")
    repository_status, repository_doc, repository_body = api_get(f"{base}/repos/{repository}", token)
    teams_status, teams_doc, teams_body = api_get(f"{base}/repos/{repository}/teams?per_page=100", token)
    commit_status, commit_doc, commit_body = api_get(f"{base}/repos/{repository}/commits/{commit}", token)
    if repository_status != 200 or not isinstance(repository_doc, dict):
        failures.append(_api_failure("repository ownership", repository_status, repository_body))
        repository_doc = None
    if teams_status != 200 or not isinstance(teams_doc, list):
        failures.append(_api_failure("repository teams", teams_status, teams_body))
        teams_doc = None
    if commit_status != 200 or not isinstance(commit_doc, dict):
        failures.append(_api_failure("checked commit", commit_status, commit_body))
        commit_doc = None

    organization = str(contract.get("required_organization") or "").strip()
    members_by_team: dict[str, list[dict[str, Any]]] = {}
    for item in contract.get("required_teams") or []:
        slug = _team_slug(item)
        if not slug:
            continue
        status, members, body = api_get(f"{base}/orgs/{organization}/teams/{slug}/members?per_page=100", token)
        if status == 200 and isinstance(members, list):
            members_by_team[slug] = members
        else:
            failures.append(_api_failure(f"members for team {slug}", status, body))

    if branch_report is None:
        branch_report = audit_branch_protection(root, repository, token, api_base, api_get=api_get)
    return evaluate_team_governance(
        root=root,
        contract=contract,
        repository=repository,
        commit=commit,
        repository_doc=repository_doc,
        repository_teams=teams_doc,
        commit_doc=commit_doc,
        members_by_team=members_by_team,
        branch_report=branch_report,
        api_failures=failures,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify live GitHub organization/team CODEOWNERS governance.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--commit", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--token-env", default="TEAM_GOVERNANCE_TOKEN")
    parser.add_argument("--api-base", default=os.environ.get("GITHUB_API_URL", GITHUB_API))
    parser.add_argument("--branch-protection-report", default="")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--write-report", default="")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    commit = args.commit or _git_head(root)
    token = os.environ.get(args.token_env, "") or os.environ.get("GITHUB_TOKEN", "")
    branch_report: dict[str, Any] | None = None
    if args.branch_protection_report:
        branch_path = Path(args.branch_protection_report)
        if not branch_path.is_absolute():
            branch_path = root / branch_path
        try:
            branch_report = _read_json(branch_path)
        except (OSError, ValueError, RuntimeError) as exc:
            branch_report = {"status": "blocked", "failures": [f"cannot read branch report: {exc}"]}
    report = audit(root, args.repository, commit, token, args.api_base, branch_report=branch_report)
    if args.write_report:
        output = Path(args.write_report)
        if not output.is_absolute():
            output = root / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "failure_count": len(report["failures"]),
                "team_count": len(report.get("required_teams") or []),
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
