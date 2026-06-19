from __future__ import annotations
import json
from pathlib import Path
class WorkflowPatchGenerator:
    def generate(self, proposal, output_root: Path) -> Path:
        path = output_root / "workflows" / f"{proposal.proposal_id}.json"; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"proposal_id": proposal.proposal_id, "title": proposal.title, "problem": proposal.problem, "workflow_change": proposal.proposed_change, "test_plan": proposal.test_plan, "rollback_plan": proposal.rollback_plan, "channel": "canary"}, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
