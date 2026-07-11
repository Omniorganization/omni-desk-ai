from __future__ import annotations

from copy import deepcopy

from scripts.check_github_branch_protection_live import evaluate_live_protection


CONTRACT = {
    "allow_deletions": False,
    "allow_direct_pushes": False,
    "allow_force_pushes": False,
    "allowed_bypass_actors": [],
    "dismiss_stale_reviews": True,
    "require_branch_up_to_date": True,
    "require_codeowners_review": True,
    "require_conversation_resolution": True,
    "require_enforce_admins": True,
    "require_last_push_approval": True,
    "require_linear_history": True,
    "require_lock_branch": False,
    "require_pull_request": True,
    "require_signed_commits": True,
    "required_approving_review_count": 1,
    "required_check_contexts": ["CI", "Security", "main-verification", "team-governance"],
    "required_deployments": [],
}


def _legacy_defaults() -> dict:
    return {
        "allow_deletions": {"enabled": False},
        "allow_force_pushes": {"enabled": False},
        "enforce_admins": {"enabled": False},
        "lock_branch": {"enabled": False},
        "required_conversation_resolution": {"enabled": False},
        "required_linear_history": {"enabled": False},
        "required_pull_request_reviews": None,
        "required_signatures": {"enabled": False},
        "required_status_checks": None,
    }


def _rules(*, contexts: list[str] | None = None) -> list[dict]:
    return [
        {"type": "deletion", "ruleset_id": 10},
        {"type": "non_fast_forward", "ruleset_id": 10},
        {"type": "required_linear_history", "ruleset_id": 10},
        {"type": "required_signatures", "ruleset_id": 10},
        {
            "type": "pull_request",
            "ruleset_id": 10,
            "parameters": {
                "dismiss_stale_reviews_on_push": True,
                "require_code_owner_review": True,
                "require_last_push_approval": True,
                "required_approving_review_count": 1,
                "required_review_thread_resolution": True,
            },
        },
        {
            "type": "required_status_checks",
            "ruleset_id": 10,
            "parameters": {
                "strict_required_status_checks_policy": True,
                "required_status_checks": [
                    {"context": value}
                    for value in (contexts or ["CI", "Security", "main-verification", "team-governance"])
                ],
            },
        },
    ]


def _rulesets(*, bypass_actors: list[dict] | None = None) -> list[dict]:
    return [{"id": 10, "enforcement": "active", "bypass_actors": bypass_actors or []}]


def _evaluate(*, rules: list[dict] | None = None, rulesets: list[dict] | None = None):
    return evaluate_live_protection(
        contract=CONTRACT,
        repository="owner/repo",
        branch="main",
        protection=_legacy_defaults(),
        signatures={"enabled": False},
        rules=_rules() if rules is None else rules,
        rulesets=_rulesets() if rulesets is None else rulesets,
    )


def test_effective_ruleset_protection_overrides_sparse_legacy_defaults() -> None:
    result = _evaluate()

    assert result.ok is True
    assert result.report["status"] == "passed"
    effective = result.report["effective_protection"]
    assert effective["strict_required_status_checks"] is True
    assert effective["signed_commits_required"] is True
    assert effective["linear_history_required"] is True
    assert effective["admins_enforced"] is True
    assert effective["bypass_actors"] == []


def test_missing_live_status_checks_are_rejected() -> None:
    result = _evaluate(rules=_rules(contexts=["Security", "team-governance"]))

    assert result.ok is False
    assert any("CI" in failure and "main-verification" in failure for failure in result.failures)


def test_strict_signed_linear_and_last_push_controls_are_independently_required() -> None:
    rules = _rules()
    rules = [rule for rule in rules if rule["type"] not in {"required_signatures", "required_linear_history"}]
    status_rule = next(rule for rule in rules if rule["type"] == "required_status_checks")
    status_rule["parameters"]["strict_required_status_checks_policy"] = False
    pull_rule = next(rule for rule in rules if rule["type"] == "pull_request")
    pull_rule["parameters"]["require_last_push_approval"] = False

    result = _evaluate(rules=rules)

    assert result.ok is False
    assert any("strict/up-to-date" in failure for failure in result.failures)
    assert any("signed-commit" in failure for failure in result.failures)
    assert any("linear-history" in failure for failure in result.failures)
    assert any("last-push" in failure for failure in result.failures)


def test_ruleset_bypass_actor_is_rejected_and_admins_are_not_considered_enforced() -> None:
    result = _evaluate(
        rulesets=_rulesets(
            bypass_actors=[{"actor_type": "OrganizationAdmin", "actor_id": 1, "bypass_mode": "always"}]
        )
    )

    assert result.ok is False
    assert any("bypass actors" in failure for failure in result.failures)
    assert any("admin enforcement" in failure for failure in result.failures)


def test_required_deployment_and_lock_branch_contracts_are_checked() -> None:
    contract = deepcopy(CONTRACT)
    contract["required_deployments"] = ["production"]
    contract["require_lock_branch"] = True
    result = evaluate_live_protection(
        contract=contract,
        repository="owner/repo",
        branch="main",
        protection=_legacy_defaults(),
        signatures={"enabled": False},
        rules=_rules(),
        rulesets=_rulesets(),
    )

    assert result.ok is False
    assert any("deployment environments" in failure for failure in result.failures)
    assert any("lock-branch" in failure for failure in result.failures)


def test_unreadable_control_plane_fails_closed() -> None:
    result = evaluate_live_protection(
        contract=CONTRACT,
        repository="owner/repo",
        branch="main",
        protection=None,
        signatures=None,
        rules=None,
        rulesets=None,
        api_failures=["cannot read live protection"],
    )

    assert result.ok is False
    assert result.report["status"] == "blocked"
