from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from omnidesk_agent.config import SandboxConfig
from omnidesk_agent.self_upgrade.generation.code_patch_generator import CodePatchGenerator
from omnidesk_agent.self_upgrade.generation.prompt_patch_generator import PromptPatchGenerator
from omnidesk_agent.self_upgrade.generation.workflow_patch_generator import WorkflowPatchGenerator
from omnidesk_agent.self_upgrade.proposal.proposal_generator import UpgradeProposalGenerator
from omnidesk_agent.self_upgrade.proposal.proposal_store import UpgradeProposalStore
from omnidesk_agent.self_upgrade.risk.risk_classifier import UpgradeRiskClassifier
from omnidesk_agent.self_upgrade.risk.permission_diff_checker import PermissionDiffChecker
from omnidesk_agent.self_upgrade.testing.regression_runner import RegressionRunner
from omnidesk_agent.self_upgrade.testing.security_test_runner import SecurityTestRunner
from omnidesk_agent.self_upgrade.release.shadow_mode import ShadowModeEvaluator
from omnidesk_agent.self_upgrade.release.canary_release import CanaryReleaseManager
from omnidesk_agent.self_upgrade.memory.upgrade_memory import UpgradeMemory
from omnidesk_agent.self_upgrade.state_machine import UpgradeStateMachine


class GovernedSelfImprovement:
    """Governed self-improvement pipeline.

    It creates proposals, scores them, checks permissions/risk, generates review
    artifacts, and uses regression/security/shadow/canary evidence before stable
    release. It never auto-merges.
    """

    def __init__(self, workspace_root: Path, repo_root: Path, sandbox_cfg: SandboxConfig | None = None):
        self.workspace_root = workspace_root.expanduser()
        self.repo_root = repo_root.resolve()
        self.proposal_store = UpgradeProposalStore(self.workspace_root / "upgrade_proposals")
        self.proposal_generator = UpgradeProposalGenerator()
        self.risk_classifier = UpgradeRiskClassifier()
        self.permission_diff = PermissionDiffChecker()
        self.sandbox_cfg = sandbox_cfg
        self.regression_runner = RegressionRunner(self.repo_root, sandbox_cfg=sandbox_cfg)
        self.security_runner = SecurityTestRunner(self.repo_root, sandbox_cfg=sandbox_cfg)
        self.shadow = ShadowModeEvaluator()
        self.canary = CanaryReleaseManager(self.workspace_root / "canary_state.json")
        self.memory = UpgradeMemory(self.workspace_root / "upgrade_memory.sqlite3")
        self.state_machine = UpgradeStateMachine()
        self.prompt_generator = PromptPatchGenerator()
        self.workflow_generator = WorkflowPatchGenerator()
        self.code_generator = CodePatchGenerator()

    def create_proposals_from_failures(self, failure_summary: list[dict[str, Any]]) -> list[dict]:
        created = []
        for item in failure_summary:
            proposal = self.proposal_generator.from_failure_summary(item)
            self.proposal_store.create(proposal)
            created.append(proposal.to_dict())
        return created

    def generate_artifact(self, proposal_id: str) -> dict:
        proposal = self.proposal_store.get(proposal_id)
        if proposal is None:
            raise KeyError(proposal_id)
        output_root = self.workspace_root / "upgrade_artifacts"
        risk = self.risk_classifier.classify(proposal)
        if proposal.upgrade_type == "prompt":
            path = self.prompt_generator.generate(proposal, output_root)
        elif proposal.upgrade_type in {"workflow", "skill", "test"}:
            path = self.workflow_generator.generate(proposal, output_root)
        else:
            path = self.code_generator.generate(proposal, output_root)

        proposal_dict = proposal.to_dict()
        metadata = self._transition_to(proposal_dict, "RISK_CLASSIFIED", reason="artifact_generation_risk_classified")
        proposal_dict["metadata"] = metadata
        metadata = self._transition_to(proposal_dict, "ARTIFACT_GENERATED", reason="artifact_generated")
        metadata.setdefault("checks", {})["risk_classification"] = risk
        metadata["risk_classification"] = risk
        metadata.setdefault("artifacts", []).append({
            "path": str(path),
            "risk": risk,
        })
        proposal.metadata = metadata
        self.proposal_store.save(proposal)
        return {"proposal": proposal.to_dict(), "risk": risk, "artifact_path": str(path)}

    async def evaluate_proposal(
        self,
        proposal_id: str,
        *,
        old_permissions: Optional[list[str]] = None,
        new_permissions: Optional[list[str]] = None,
        stable_plan: Optional[dict[str, Any]] = None,
        shadow_plan: Optional[dict[str, Any]] = None,
        allow_canary: bool = False,
    ) -> dict:
        """Run governance gates and write results back into proposal metadata.

        Evidence written:
          - permission_diff
          - risk_classification
          - regression_result
          - security_result
          - shadow_result
          - canary_result

        Promotion policy:
          - Can enable canary only when owner-authorized, risk classifier says
            low-risk canary is allowed, and regression/security checks passed.
          - Never auto-merges and never promotes to stable.
        """

        proposal = self.proposal_store.get(proposal_id)
        if proposal is None:
            raise KeyError(proposal_id)

        perm_diff = self.permission_diff.compare(old_permissions or [], new_permissions or [])
        risk = self.risk_classifier.classify(proposal, permission_diff=perm_diff.to_dict())
        regression = await self.regression_runner.run()
        security = await self.security_runner.run()

        shadow_result = None
        if stable_plan is not None and shadow_plan is not None:
            shadow_result = self.shadow.compare_plans(
                task=proposal.title,
                stable_plan=stable_plan,
                shadow_plan=shadow_plan,
            ).to_dict()

        canary_result = {
            "enabled": False,
            "reason": "not eligible",
        }
        checks_ok = bool(regression.get("ok")) and bool(security.get("ok"))
        if allow_canary and checks_ok and risk.get("can_auto_canary") and not perm_diff.requires_human_approval:
            canary_result = self.canary.enable(
                target=proposal.proposal_id,
                version=f"proposal-{proposal.proposal_id}",
                allowed_risk="low",
            )
            canary_result["enabled"] = True
        elif not allow_canary:
            canary_result["reason"] = "canary_requires_owner_authorization"
        elif not checks_ok:
            canary_result["reason"] = "regression_or_security_check_failed"
        elif perm_diff.requires_human_approval:
            canary_result["reason"] = "permission_diff_requires_human_approval"
        elif not risk.get("can_auto_canary"):
            canary_result["reason"] = "risk_classifier_disallows_auto_canary"

        checks = {
            "permission_diff": perm_diff.to_dict(),
            "risk_classification": risk,
            "regression": regression,
            "security": security,
            "shadow": shadow_result,
            "canary": canary_result,
        }
        evidence = {
            "permission_diff": perm_diff.to_dict(),
            "risk_classification": risk,
            "regression_result": regression,
            "security_result": security,
            "shadow_result": shadow_result,
            "canary_result": canary_result,
            "checks": checks,
        }

        proposal.metadata["governance_evaluation"] = evidence
        proposal.metadata["checks"] = checks
        target_state = "CANARY" if canary_result.get("enabled") else ("BLOCKED" if not checks_ok else "HUMAN_REVIEW")
        proposal.metadata = self._transition_governance_path(proposal.to_dict(), target_state)
        proposal.metadata["governance_evaluation"] = evidence
        proposal.metadata["checks"] = checks
        self.proposal_store.save(proposal)

        verdict = "effective" if canary_result.get("enabled") else "pending_review"
        if perm_diff.requires_human_approval or risk.get("requires_human_approval"):
            verdict = "requires_human_approval"
        if not checks_ok:
            verdict = "blocked_by_tests"

        self.memory.record({
            "upgrade_id": proposal.proposal_id,
            "change_type": proposal.upgrade_type,
            "target": ",".join(proposal.affected_modules) or proposal.title,
            "rollback": False,
            "verdict": verdict,
            "metadata": evidence,
        })

        return {"proposal": proposal.to_dict(), "evaluation": evidence, "verdict": verdict}


    def _transition_to(self, proposal_dict: dict, target_state: str, *, reason: str) -> dict:
        metadata = dict(proposal_dict.get("metadata") or {})
        current = metadata.get("state") or "PROPOSED"
        if current == target_state:
            return metadata
        working = dict(proposal_dict)
        working["metadata"] = metadata
        return self.state_machine.transition_metadata(working, target_state, reason=reason)

    def _transition_governance_path(self, proposal_dict: dict, target_state: str) -> dict:
        metadata = dict(proposal_dict.get("metadata") or {})
        current = metadata.get("state") or "PROPOSED"
        if current == target_state:
            return metadata
        canonical_paths = {
            "HUMAN_REVIEW": ["PROPOSED", "RISK_CLASSIFIED", "ARTIFACT_GENERATED", "REGRESSION_TESTED", "SECURITY_TESTED", "HUMAN_REVIEW"],
            "CANARY": ["PROPOSED", "RISK_CLASSIFIED", "ARTIFACT_GENERATED", "REGRESSION_TESTED", "SECURITY_TESTED", "SHADOW_MODE", "CANARY"],
        }
        if target_state == "BLOCKED":
            if self.state_machine.can_transition(current, "BLOCKED"):
                working = dict(proposal_dict)
                working["metadata"] = metadata
                return self.state_machine.transition_metadata(working, "BLOCKED", reason="governance_evaluation_blocked")
            metadata["state"] = "BLOCKED"
            metadata.setdefault("state_history", []).append({"from": current, "to": "BLOCKED", "reason": "governance_evaluation_forced_block"})
            return metadata

        sequence = canonical_paths.get(target_state, [current, target_state])
        if current not in sequence:
            raise ValueError(f"cannot continue governance path from {current} to {target_state}")
        start = sequence.index(current) + 1
        for state in sequence[start:]:
            working = dict(proposal_dict)
            working["metadata"] = metadata
            metadata = self.state_machine.transition_metadata(working, state, reason="governance_evaluation")
            proposal_dict = dict(proposal_dict)
            proposal_dict["metadata"] = metadata
            if state == target_state:
                return metadata
        return metadata

    def record_human_feedback(self, proposal_id: str, decision: str, reason: str) -> dict:
        if decision == "approved":
            proposal = self.proposal_store.approve(proposal_id, reason)
            proposal.metadata.setdefault("checks", {})["human_review"] = {"decision": "approved", "reason": reason}
            proposal.metadata["human_review"] = {"decision": "approved", "reason": reason}
            self.proposal_store.save(proposal)
        elif decision == "rejected":
            proposal = self.proposal_store.reject(proposal_id, reason)
            proposal.metadata.setdefault("checks", {})["human_review"] = {"decision": "rejected", "reason": reason}
            proposal.metadata["human_review"] = {"decision": "rejected", "reason": reason}
            self.proposal_store.save(proposal)
        else:
            raise ValueError("decision must be approved or rejected")
        self.memory.record({
            "upgrade_id": proposal_id,
            "change_type": proposal.upgrade_type,
            "target": ",".join(proposal.affected_modules) or proposal.title,
            "rollback": False,
            "verdict": decision,
            "human_feedback": reason,
        })
        return proposal.to_dict()

    def close(self) -> None:
        close = getattr(self.memory, "close", None)
        if callable(close):
            close()
