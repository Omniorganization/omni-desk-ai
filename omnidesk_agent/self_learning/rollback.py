from __future__ import annotations

import time
from typing import Optional

from omnidesk_agent.self_learning.schemas import LearningProposal, RollbackRecord
from omnidesk_agent.self_learning.store import SelfLearningStore


class RollbackManager:
    """Create and record rollback plans for controlled self-learning changes."""

    def __init__(self, store: Optional[SelfLearningStore] = None):
        self.store = store

    def plan(self, proposal: LearningProposal, *, target: Optional[str] = None) -> RollbackRecord:
        plan = proposal.rollback_plan or "Disable the proposed change and restore the previous approved version."
        rollback = RollbackRecord(
            proposal_id=proposal.proposal_id,
            target=target or ",".join(proposal.affected_modules) or proposal.proposal_type,
            plan=plan,
            status="planned",
            metadata={"stage": proposal.stage, "proposal_type": proposal.proposal_type},
        )
        self._save(rollback)
        return rollback

    def record_execution(self, rollback: RollbackRecord, *, ok: bool, reason: str = "") -> RollbackRecord:
        rollback.status = "completed" if ok else "failed"
        rollback.executed_at = time.time()
        rollback.metadata["execution_reason"] = reason
        self._save(rollback)
        return rollback

    def _save(self, rollback: RollbackRecord) -> None:
        if self.store is not None:
            self.store.save_rollback(rollback)
