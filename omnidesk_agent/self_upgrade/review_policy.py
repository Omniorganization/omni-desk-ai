from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


FORBIDDEN_REPAIR_CHANGES = (
    "lower_security_policy",
    "bypass_approval",
    "expand_token_scope",
    "delete_external_evidence_gate",
    "mark_audit_only_as_passed",
    "fabricate_release_evidence",
)


@dataclass(frozen=True)
class ReviewPolicyDecision:
    allowed: bool
    blockers: tuple[str, ...]
    required_sections: tuple[str, ...] = ("tests", "rollback_plan", "evidence_bundle", "risk_assessment")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_repair_policy(change_types: tuple[str, ...], *, has_tests: bool, has_rollback: bool) -> ReviewPolicyDecision:
    blockers = [change for change in change_types if change in FORBIDDEN_REPAIR_CHANGES]
    if not has_tests:
        blockers.append("missing_tests")
    if not has_rollback:
        blockers.append("missing_rollback_plan")
    return ReviewPolicyDecision(allowed=not blockers, blockers=tuple(blockers))
