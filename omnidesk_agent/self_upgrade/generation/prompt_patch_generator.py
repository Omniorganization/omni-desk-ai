from __future__ import annotations
from pathlib import Path
class PromptPatchGenerator:
    def generate(self, proposal, output_root: Path) -> Path:
        path = output_root / "prompts" / f"{proposal.proposal_id}.md"; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Prompt Upgrade: {proposal.title}\n\nProblem: {proposal.problem}\n\nProposed strategy:\n{proposal.proposed_change}\n\nStatus: canary only until approved.\n", encoding="utf-8")
        return path
