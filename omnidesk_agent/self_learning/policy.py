from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from omnidesk_agent.self_learning.schemas import LearningProposal


STAGE_OBSERVE = "stage_1_observe"
STAGE_KNOWLEDGE_PROMPT = "stage_2_knowledge_prompt"
STAGE_CODE_PR = "stage_3_code_pr"

KNOWLEDGE_PROMPT_TYPES = {"knowledge_update", "prompt_template", "workflow_rule"}
CODE_PR_TYPES = {"code_fix", "test_improvement"}

FORBIDDEN_METADATA_FLAGS = {
    "auto_merge",
    "direct_main_patch",
    "direct_production_write",
    "bypass_approval",
    "delete_audit_log",
    "disable_rollback",
}

FORBIDDEN_CONTENT_PATTERNS = {
    "auto_merge": (
        "auto merge",
        "auto-merge",
        "automatically merge",
        "merge without review",
        "merge without approval",
    ),
    "direct_main_patch": (
        "direct main patch",
        "patch main directly",
        "write directly to main",
        "commit directly to main",
    ),
    "direct_production_write": (
        "direct production write",
        "write directly to production",
        "apply directly to production",
        "push directly to production",
        "production without staging",
    ),
    "bypass_approval": (
        "bypass approval",
        "skip approval",
        "without approval",
        "no human approval",
        "avoid approval",
    ),
    "delete_audit_log": (
        "delete audit log",
        "delete audit logs",
        "remove audit log",
        "erase audit log",
        "clear audit history",
    ),
    "disable_rollback": (
        "disable rollback",
        "without rollback",
        "skip rollback",
        "no rollback",
    ),
}


@dataclass(frozen=True)
class SelfLearningPolicyDecision:
    allowed: bool
    mode: str
    requires_human_approval: bool
    requires_pr: bool
    blockers: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SelfLearningBoundaryPolicy:
    """Fail-closed policy for proactive self-learning.

    Stage 1 may observe and draft only. Stage 2 may stage knowledge, prompt and
    workflow updates only after approval. Stage 3 may prepare code-repair PRs
    on ai/* branches but never merges them.
    """

    def evaluate(self, proposal: LearningProposal) -> SelfLearningPolicyDecision:
        blockers: list[str] = []
        metadata = proposal.metadata or {}
        for flag in FORBIDDEN_METADATA_FLAGS:
            if metadata.get(flag):
                blockers.append(flag)

        self._append_content_blockers(proposal, blockers)

        if proposal.stage == STAGE_OBSERVE:
            if metadata.get("applies_system_change"):
                blockers.append("stage_1_cannot_apply_system_change")
            return self._decision(
                not blockers,
                "observe_and_draft_only",
                False,
                False,
                blockers,
                "stage 1 can collect, analyze and draft proposals only",
            )

        if proposal.stage == STAGE_KNOWLEDGE_PROMPT:
            if proposal.proposal_type not in KNOWLEDGE_PROMPT_TYPES:
                blockers.append("stage_2_only_updates_knowledge_prompt_or_workflow")
            return self._decision(
                not blockers,
                "approval_required_before_staging",
                True,
                False,
                blockers,
                "stage 2 updates require human approval before staging or production",
            )

        if proposal.stage == STAGE_CODE_PR:
            if proposal.proposal_type not in CODE_PR_TYPES:
                blockers.append("stage_3_only_code_or_test_prs")
            branch = proposal.branch_name or str(metadata.get("branch_name") or "")
            if branch and not branch.startswith("ai/"):
                blockers.append("code_repair_branch_must_start_with_ai")
            if metadata.get("merge_after_create"):
                blockers.append("stage_3_must_not_merge_pr")
            return self._decision(
                not blockers,
                "pr_only_no_merge",
                True,
                True,
                blockers,
                "stage 3 may generate code-fix PRs for review but must not merge",
            )

        blockers.append("unknown_self_learning_stage")
        return self._decision(False, "blocked", True, True, blockers, "unknown self-learning stage")

    def assert_allowed(self, proposal: LearningProposal) -> SelfLearningPolicyDecision:
        decision = self.evaluate(proposal)
        if not decision.allowed:
            raise PermissionError("; ".join(decision.blockers) or decision.reason)
        return decision

    @staticmethod
    def _append_content_blockers(proposal: LearningProposal, blockers: list[str]) -> None:
        text_parts = [
            proposal.title,
            proposal.problem,
            proposal.proposed_change,
            proposal.expected_benefit,
            proposal.rollback_plan,
            " ".join(proposal.test_plan),
            " ".join(proposal.affected_modules),
        ]
        text = "\n".join(str(part or "") for part in text_parts).lower()
        for blocker, patterns in FORBIDDEN_CONTENT_PATTERNS.items():
            if any(pattern in text for pattern in patterns) and blocker not in blockers:
                blockers.append(blocker)

    @staticmethod
    def _decision(
        allowed: bool,
        mode: str,
        requires_human_approval: bool,
        requires_pr: bool,
        blockers: list[str],
        reason: str,
    ) -> SelfLearningPolicyDecision:
        return SelfLearningPolicyDecision(
            allowed=allowed,
            mode=mode,
            requires_human_approval=requires_human_approval,
            requires_pr=requires_pr,
            blockers=tuple(blockers),
            reason=reason,
        )
