from __future__ import annotations

from pathlib import Path

from scripts.check_github_team_governance_live import evaluate_team_governance


CONTRACT = {
    "required_organization": "Omniorganization",
    "codeowners_file": ".github/CODEOWNERS",
    "minimum_members_per_team": 2,
    "minimum_independent_reviewers_per_team": 1,
    "required_teams": [
        {"slug": "omnidesk-maintainers", "minimum_repository_permission": "maintain"},
        {"slug": "release-owners", "minimum_repository_permission": "push"},
        {"slug": "security-owners", "minimum_repository_permission": "push"},
        {"slug": "platform-owners", "minimum_repository_permission": "push"},
    ],
}


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".github").mkdir(parents=True)
    (root / ".github/CODEOWNERS").write_text(
        "\n".join(
            [
                "* @Omniorganization/omnidesk-maintainers",
                "/.github/ @Omniorganization/release-owners @Omniorganization/security-owners",
                "/deploy/ @Omniorganization/platform-owners",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def _teams() -> list[dict]:
    return [
        {"slug": "omnidesk-maintainers", "permission": "maintain", "privacy": "closed"},
        {"slug": "release-owners", "permission": "push", "privacy": "closed"},
        {"slug": "security-owners", "permission": "push", "privacy": "closed"},
        {"slug": "platform-owners", "permission": "push", "privacy": "closed"},
    ]


def _members(*, separated: bool = True) -> dict[str, list[dict]]:
    members = [{"login": "author"}]
    if separated:
        members.append({"login": "reviewer"})
    return {str(team["slug"]): list(members) for team in _teams()}


def _branch_report(*, bypass: bool = False) -> dict:
    return {
        "status": "passed",
        "effective_protection": {
            "code_owner_review_required": True,
            "admins_enforced": True,
            "bypass_actors": ["Team:1:always"] if bypass else [],
        },
    }


def _evaluate(tmp_path: Path, **overrides):
    root = overrides.pop("root", None) or _root(tmp_path)
    values = {
        "root": root,
        "contract": CONTRACT,
        "repository": "Omniorganization/omni-desk-ai",
        "commit": "abc1234",
        "repository_doc": {"owner": {"login": "Omniorganization", "type": "Organization"}},
        "repository_teams": _teams(),
        "commit_doc": {"author": {"login": "author"}},
        "members_by_team": _members(),
        "branch_report": _branch_report(),
    }
    values.update(overrides)
    return evaluate_team_governance(**values)


def test_live_team_governance_passes_with_independent_reviewers(tmp_path: Path) -> None:
    report = _evaluate(tmp_path)

    assert report["status"] == "passed"
    assert report["required_teams_resolved"] is True
    assert report["required_team_member_separation_satisfied"] is True
    assert all(team["valid"] for team in report["required_teams"])


def test_live_team_governance_blocks_one_person_team_shells(tmp_path: Path) -> None:
    report = _evaluate(tmp_path, members_by_team=_members(separated=False))

    assert report["status"] == "blocked"
    assert report["required_team_member_separation_satisfied"] is False
    assert any("at least 2 are required" in failure for failure in report["failures"])
    assert any("independent of commit author" in failure for failure in report["failures"])


def test_live_team_governance_blocks_weak_permission_and_secret_team(tmp_path: Path) -> None:
    teams = _teams()
    teams[0]["permission"] = "push"
    teams[1]["privacy"] = "secret"

    report = _evaluate(tmp_path, repository_teams=teams)

    assert report["status"] == "blocked"
    assert any("weaker than maintain" in failure for failure in report["failures"])
    assert any("closed/visible" in failure for failure in report["failures"])


def test_live_team_governance_blocks_personal_codeowner_and_ruleset_bypass(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with (root / ".github/CODEOWNERS").open("a", encoding="utf-8") as handle:
        handle.write("/scripts/ @personal-owner\n")

    report = _evaluate(tmp_path, root=root, branch_report=_branch_report(bypass=True))

    assert report["status"] == "blocked"
    assert report["personal_owner_fallback_active"] is True
    assert any("personal or unauthorized owners" in failure for failure in report["failures"])
    assert any("bypass actors" in failure for failure in report["failures"])


def test_live_team_governance_fails_closed_when_api_is_unreadable(tmp_path: Path) -> None:
    report = _evaluate(
        tmp_path,
        repository_doc=None,
        repository_teams=None,
        commit_doc=None,
        members_by_team={},
        api_failures=["cannot read repository teams: HTTP 403"],
    )

    assert report["status"] == "blocked"
    assert "cannot read repository teams: HTTP 403" in report["failures"]
