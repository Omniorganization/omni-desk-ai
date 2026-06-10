from __future__ import annotations
from pathlib import Path
from typing import Any
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

class GovernedSelfImprovement:
    def __init__(self, workspace_root: Path, repo_root: Path):
        self.workspace_root=workspace_root.expanduser(); self.repo_root=repo_root.resolve()
        self.proposal_store=UpgradeProposalStore(self.workspace_root/"upgrade_proposals")
        self.proposal_generator=UpgradeProposalGenerator(); self.risk_classifier=UpgradeRiskClassifier(); self.permission_diff=PermissionDiffChecker()
        self.regression_runner=RegressionRunner(self.repo_root); self.security_runner=SecurityTestRunner(self.repo_root)
        self.shadow=ShadowModeEvaluator(); self.canary=CanaryReleaseManager(self.workspace_root/"canary_state.json")
        self.memory=UpgradeMemory(self.workspace_root/"upgrade_memory.sqlite3")
        self.prompt_generator=PromptPatchGenerator(); self.workflow_generator=WorkflowPatchGenerator(); self.code_generator=CodePatchGenerator()
    def create_proposals_from_failures(self, failure_summary: list[dict[str, Any]]) -> list[dict]:
        created=[]
        for item in failure_summary:
            proposal=self.proposal_generator.from_failure_summary(item); self.proposal_store.create(proposal); created.append(proposal.to_dict())
        return created
    def generate_artifact(self, proposal_id: str) -> dict:
        proposal=self.proposal_store.get(proposal_id)
        if proposal is None: raise KeyError(proposal_id)
        output_root=self.workspace_root/"upgrade_artifacts"; risk=self.risk_classifier.classify(proposal)
        if proposal.upgrade_type=="prompt": path=self.prompt_generator.generate(proposal, output_root)
        elif proposal.upgrade_type in {"workflow","skill","test"}: path=self.workflow_generator.generate(proposal, output_root)
        else: path=self.code_generator.generate(proposal, output_root)
        return {"proposal":proposal.to_dict(),"risk":risk,"artifact_path":str(path)}
    def record_human_feedback(self, proposal_id: str, decision: str, reason: str) -> dict:
        if decision=="approved": proposal=self.proposal_store.approve(proposal_id, reason)
        elif decision=="rejected": proposal=self.proposal_store.reject(proposal_id, reason)
        else: raise ValueError("decision must be approved or rejected")
        self.memory.record({"upgrade_id":proposal_id,"change_type":proposal.upgrade_type,"target":",".join(proposal.affected_modules) or proposal.title,"rollback":False,"verdict":decision,"human_feedback":reason})
        return proposal.to_dict()
