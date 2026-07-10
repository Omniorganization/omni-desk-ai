from __future__ import annotations

from scripts.check_github_branch_protection_live import evaluate_live_protection


CONTRACT = {
    "allow_deletions": False,
    "allow_direct_pushes": False,
    "allow_force_pushes": False,
    "base_branch": "main",
    "dismiss_stale_reviews": True,
    "require_codeowners_review": True,
    "require_conversation_resolution": True,
    "require_pull_request": True,
    "required_approving_review_count": 1,
    "required_status_checks": ["CI workflow", "Security workflow"],
    "required_check_contexts": ["CI", "Security", "release-policy", "external-ga-evidence-contract"],
}


def _protected_payload(*, contexts: list[str] | None = None) -> dict:
    return {
        "allow_deletions": {"enabled": False},
        "allow_force_pushes": {"enabled": False},
        "enforce_admins": {"enabled": True},
        "required_conversation_resolution": {"enabled": True},
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": True,
            "required_approving_review_count": 1,
        },
        "required_status_checks": {
            "contexts": contexts
            or ["CI", "Security", "release-policy", "external-ga-evidence-contract"],
            "strict": True,
        },
    }


def test_live_branch_protection_reports_unprotected_branch() -> None:
    result = evaluate_live_protection(
        contract=CONTRACT,
        protection_status=404,
        protection={"message": "Branch not protected"},
        repo="owner/repo",
        branch="main",
    )

    assert result.ok is False
    assert result.report["status"] == "not_protected"
    assert "not enabled" in result.failures[0]


def test_live_branch_protection_accepts_matching_contract() -> None:
    result = evaluate_live_protection(
        contract=CONTRACT,
        protection_status=200,
        protection=_protected_payload(),
        repo="owner/repo",
        branch="main",
    )

    assert result.ok is True
    assert result.report["status"] == "passed"
    assert result.report["required_status_checks"] == [
        "CI",
        "Security",
        "external-ga-evidence-contract",
        "release-policy",
    ]


def test_live_branch_protection_rejects_missing_status_checks() -> None:
    result = evaluate_live_protection(
        contract=CONTRACT,
        protection_status=200,
        protection=_protected_payload(contexts=["Security", "release-policy"]),
        repo="owner/repo",
        branch="main",
    )

    assert result.ok is False
    assert result.report["status"] == "failed"
    assert "CI" in result.failures[0]
    assert "external-ga-evidence-contract" in result.failures[0]
