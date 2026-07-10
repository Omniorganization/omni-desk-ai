#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REQUIRED_TEAMS = {
    "omnidesk-maintainers",
    "release-owners",
    "security-owners",
    "platform-owners",
}

REQUIRED_PATHS = {
    "*",
    ".github/workflows/",
    "scripts/",
    "deploy/",
    "release/",
    "omnidesk_agent/security/",
    "omnidesk_agent/sandbox/",
    "omnidesk_agent/plugins/",
    "omnidesk_agent/self_upgrade/",
}


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"missing team governance contract: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _check(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate source-side team governance contract.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    contract_path = root / ".github" / "team-governance.required.json"
    contract = _load_json(contract_path)
    codeowners_path = root / str(contract.get("codeowners_file") or ".github/CODEOWNERS")
    failures: list[str] = []

    _check(contract.get("schema") == "omnidesk-team-governance/v1", "team governance schema must be omnidesk-team-governance/v1", failures)
    _check(
        contract.get("repository_full_name") == "Omniorganization/omni-desk-ai",
        "team governance repository_full_name must be Omniorganization/omni-desk-ai",
        failures,
    )
    _check(contract.get("required_for_customer_distribution_ga") is True, "team governance must be required for customer-distribution GA", failures)
    _check(contract.get("required_repository_owner_type") == "Organization", "customer-distribution GA must require an Organization-owned repository", failures)
    _check(contract.get("personal_owner_fallback_forbidden_for_real_ga") is True, "personal owner fallback must be forbidden for Real GA", failures)
    _check(bool(contract.get("live_evidence_required_file")), "live team governance evidence file must be declared", failures)
    owner_type = str(contract.get("current_repository_owner_type") or "").strip()
    if owner_type == "Organization":
        _check(contract.get("migration_status") == "completed", "organization migration_status must be completed", failures)
        _check(contract.get("migration_blocker") == "", "completed organization migration must not retain a blocker", failures)
        _check(
            contract.get("personal_owner_fallback_allowed_for_source_candidate") is False,
            "organization-owned repository must forbid personal fallback for source candidates",
            failures,
        )
    else:
        _check(bool(contract.get("migration_blocker")), "migration blocker must be explicit while repository is not organization-owned", failures)
    _check(contract.get("required_organization") == "Omniorganization", "required_organization must be Omniorganization", failures)

    teams = contract.get("required_teams") or []
    team_slugs = {str(item.get("slug") or "") for item in teams if isinstance(item, dict)}
    _check(REQUIRED_TEAMS.issubset(team_slugs), f"required teams missing: {sorted(REQUIRED_TEAMS - team_slugs)}", failures)

    declared_paths: set[str] = set()
    for item in teams:
        if not isinstance(item, dict):
            failures.append("required_teams entries must be objects")
            continue
        for path in item.get("required_paths") or []:
            declared_paths.add(str(path))
    _check(REQUIRED_PATHS.issubset(declared_paths), f"team governance paths missing: {sorted(REQUIRED_PATHS - declared_paths)}", failures)

    if not codeowners_path.exists():
        failures.append(f"CODEOWNERS file is missing: {codeowners_path.relative_to(root)}")
    else:
        codeowners = codeowners_path.read_text(encoding="utf-8")
        has_personal_fallback = "@yinyufan0813-cmyk" in codeowners
        if owner_type == "User":
            _check("TEAM_OWNER_BLOCKER" in codeowners, "CODEOWNERS must document the personal-repository team blocker", failures)
            _check(has_personal_fallback, "source-candidate personal fallback owner must remain explicit until org migration", failures)
        elif owner_type == "Organization":
            _check(
                not has_personal_fallback,
                "organization-owned Real GA CODEOWNERS must not keep the personal fallback owner",
                failures,
            )
        for team in REQUIRED_TEAMS:
            _check(
                f"@Omniorganization/{team}" in codeowners,
                f"CODEOWNERS migration target missing team: {team}",
                failures,
            )

    if failures:
        print("team governance contract check failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("team governance contract ok; Real GA still requires live organization/team evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
