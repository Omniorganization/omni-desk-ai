from __future__ import annotations
from pathlib import Path
class CodePatchGenerator:
    def generate(self, proposal, output_root: Path) -> Path:
        path = output_root / "code_reviews" / f"{proposal.proposal_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Code Upgrade Proposal: {proposal.title}\n\nRisk: {proposal.risk_level}\n\nAffected modules: {', '.join(proposal.affected_modules)}\n\nProblem:\n{proposal.problem}\n\nProposed change:\n{proposal.proposed_change}\n\nTest plan:\n" + "\n".join(f"- {t}" for t in proposal.test_plan) + f"\n\nRollback:\n{proposal.rollback_plan}\n\nThis is a review artifact. Core code must not be changed without human approval.\n", encoding="utf-8")
        return path
