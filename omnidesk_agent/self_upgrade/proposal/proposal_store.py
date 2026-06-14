from __future__ import annotations
from typing import Optional
import json
import time
from pathlib import Path
from typing import Any
from omnidesk_agent.self_upgrade.proposal.proposal_schema import UpgradeProposal

class UpgradeProposalStore:
    STATUSES = {"pending", "approved", "rejected", "implemented"}

    def __init__(self, root: Path):
        self.root = root.expanduser()
        self.metrics: Any = None
        for status in sorted(self.STATUSES):
            (self.root / status).mkdir(parents=True, exist_ok=True)

    def create(self, proposal: UpgradeProposal) -> Path:
        proposal.status = "pending"
        proposal.updated_at = time.time()
        path = self._path(proposal.proposal_id, "pending")
        path.write_text(json.dumps(proposal.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        self._metric("omnidesk_self_upgrade_proposals_total", status=proposal.status, upgrade_type=proposal.upgrade_type)
        return path

    def get(self, proposal_id: str) -> Optional[UpgradeProposal]:
        for status in self.STATUSES:
            path = self._path(proposal_id, status)
            if path.exists():
                return UpgradeProposal.from_dict(json.loads(path.read_text(encoding="utf-8")))
        return None

    def list(self, status: Optional[str] = None) -> list[UpgradeProposal]:
        statuses = [status] if status else sorted(self.STATUSES)
        proposals = []
        for st in statuses:
            if st not in self.STATUSES:
                raise ValueError(f"unknown proposal status: {st}")
            for path in sorted((self.root / st).glob("*.json")):
                proposals.append(UpgradeProposal.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        return sorted(proposals, key=lambda p: (p.score, p.created_at), reverse=True)


    def save(self, proposal: UpgradeProposal) -> Path:
        if proposal.status not in self.STATUSES:
            raise ValueError(f"unknown proposal status: {proposal.status}")
        proposal.updated_at = time.time()
        path = self._path(proposal.proposal_id, proposal.status)
        path.write_text(json.dumps(proposal.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def attach_artifacts(
        self,
        proposal_id: str,
        *,
        artifact_hash: Optional[str] = None,
        artifact_sha256: Optional[str] = None,
        branch_name: Optional[str] = None,
        test_report_path: Optional[str] = None,
        regression_report_path: Optional[str] = None,
        security_report_path: Optional[str] = None,
        pr_url: Optional[str] = None,
        pr_number: Optional[int] = None,
        merge_sha: Optional[str] = None,
        merge_commit_sha: Optional[str] = None,
        approved_by: Optional[str] = None,
        approved_at: Optional[float] = None,
        rollback_artifact_path: Optional[str] = None,
    ) -> UpgradeProposal:
        proposal = self.get(proposal_id)
        if proposal is None:
            raise KeyError(proposal_id)
        if artifact_hash is not None:
            proposal.artifact_hash = artifact_hash
        if artifact_sha256 is not None:
            proposal.artifact_sha256 = artifact_sha256
        if branch_name is not None:
            proposal.branch_name = branch_name
        if test_report_path is not None:
            proposal.test_report_path = test_report_path
        if regression_report_path is not None:
            proposal.regression_report_path = regression_report_path
        if security_report_path is not None:
            proposal.security_report_path = security_report_path
        if pr_url is not None:
            proposal.pr_url = pr_url
        if pr_number is not None:
            proposal.pr_number = pr_number
        if merge_sha is not None:
            proposal.merge_sha = merge_sha
        if merge_commit_sha is not None:
            proposal.merge_commit_sha = merge_commit_sha
        if approved_by is not None:
            proposal.approved_by = approved_by
        if approved_at is not None:
            proposal.approved_at = approved_at
        if rollback_artifact_path is not None:
            proposal.rollback_artifact_path = rollback_artifact_path
        self.save(proposal)
        self._metric("omnidesk_self_upgrade_artifacts_total", status=proposal.status)
        return proposal

    def update_metadata(self, proposal_id: str, key: str, value: Any) -> UpgradeProposal:
        proposal = self.get(proposal_id)
        if proposal is None:
            raise KeyError(proposal_id)
        proposal.metadata[key] = value
        self.save(proposal)
        return proposal

    def transition(self, proposal_id: str, status: str, note: Optional[str] = None) -> UpgradeProposal:
        if status not in self.STATUSES:
            raise ValueError(f"unknown proposal status: {status}")
        proposal = self.get(proposal_id)
        if proposal is None:
            raise KeyError(proposal_id)
        old_path = self._path(proposal_id, proposal.status)
        proposal.status = status
        proposal.updated_at = time.time()
        if note:
            proposal.metadata.setdefault("history", []).append({"at": proposal.updated_at, "status": status, "note": note})
        new_path = self._path(proposal_id, status)
        new_path.write_text(json.dumps(proposal.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        if old_path.exists() and old_path != new_path:
            old_path.unlink()
        return proposal

    def approve(self, proposal_id: str, note: Optional[str] = None) -> UpgradeProposal:
        return self.transition(proposal_id, "approved", note=note)

    def reject(self, proposal_id: str, reason: str) -> UpgradeProposal:
        proposal = self.transition(proposal_id, "rejected", note=reason)
        feedback_path = self.root / "human_feedback.jsonl"
        with feedback_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"proposal_id": proposal_id, "decision": "rejected", "user_reason": reason, "future_rule": self._infer_future_rule(reason), "created_at": time.time()}, ensure_ascii=False) + "\n")
        return proposal

    def mark_implemented(self, proposal_id: str, note: Optional[str] = None) -> UpgradeProposal:
        return self.transition(proposal_id, "implemented", note=note)

    def _metric(self, name: str, **labels: Any) -> None:
        metrics = getattr(self, "metrics", None)
        inc = getattr(metrics, "inc", None)
        if callable(inc):
            inc(name, **labels)

    def _path(self, proposal_id: str, status: str) -> Path:
        return self.root / status / f"{proposal_id.replace('/', '_')}.json"

    @staticmethod
    def _infer_future_rule(reason: str) -> str:
        lower = reason.lower()
        if "email" in lower or "gmail" in lower or "邮件" in lower:
            return "email sending must always require approval"
        if "permission" in lower or "权限" in lower:
            return "permission expansion must require explicit human approval"
        if "risk" in lower or "风险" in lower:
            return "similar high-risk upgrades should be downgraded or rejected"
        return "avoid proposing similar rejected upgrade without new evidence"
