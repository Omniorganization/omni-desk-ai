from __future__ import annotations
import json, time
from pathlib import Path
from omnidesk_agent.self_upgrade.proposal.proposal_schema import UpgradeProposal

class UpgradeProposalStore:
    STATUSES = {"pending", "approved", "rejected", "implemented"}

    def __init__(self, root: Path):
        self.root = root.expanduser()
        for status in sorted(self.STATUSES):
            (self.root / status).mkdir(parents=True, exist_ok=True)

    def create(self, proposal: UpgradeProposal) -> Path:
        proposal.status = "pending"
        proposal.updated_at = time.time()
        path = self._path(proposal.proposal_id, "pending")
        path.write_text(json.dumps(proposal.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def get(self, proposal_id: str) -> UpgradeProposal | None:
        for status in self.STATUSES:
            path = self._path(proposal_id, status)
            if path.exists():
                return UpgradeProposal.from_dict(json.loads(path.read_text(encoding="utf-8")))
        return None

    def list(self, status: str | None = None) -> list[UpgradeProposal]:
        statuses = [status] if status else sorted(self.STATUSES)
        proposals = []
        for st in statuses:
            if st not in self.STATUSES:
                raise ValueError(f"unknown proposal status: {st}")
            for path in sorted((self.root / st).glob("*.json")):
                proposals.append(UpgradeProposal.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        return sorted(proposals, key=lambda p: (p.score, p.created_at), reverse=True)

    def transition(self, proposal_id: str, status: str, note: str | None = None) -> UpgradeProposal:
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

    def approve(self, proposal_id: str, note: str | None = None) -> UpgradeProposal:
        return self.transition(proposal_id, "approved", note=note)

    def reject(self, proposal_id: str, reason: str) -> UpgradeProposal:
        proposal = self.transition(proposal_id, "rejected", note=reason)
        feedback_path = self.root / "human_feedback.jsonl"
        with feedback_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"proposal_id": proposal_id, "decision": "rejected", "user_reason": reason, "future_rule": self._infer_future_rule(reason), "created_at": time.time()}, ensure_ascii=False) + "\n")
        return proposal

    def mark_implemented(self, proposal_id: str, note: str | None = None) -> UpgradeProposal:
        return self.transition(proposal_id, "implemented", note=note)

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
