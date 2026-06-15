from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from omnidesk_agent.self_upgrade.evidence_bundle import EvidenceBundle
from omnidesk_agent.self_upgrade.review_policy import evaluate_repair_policy


@dataclass(frozen=True)
class PullRequestDraft:
    title: str
    body: str
    base: str
    head: str
    labels: tuple[str, ...]
    ready_for_review: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PRGenerator:
    def draft(
        self,
        *,
        incident_id: str,
        branch: str,
        summary: str,
        bundle: EvidenceBundle,
        change_types: tuple[str, ...] = (),
        base: str = "main",
    ) -> PullRequestDraft:
        policy = evaluate_repair_policy(change_types, has_tests=bool(bundle.tests), has_rollback=bool(bundle.rollback_plan))
        body = "\n".join(
            [
                f"## Incident",
                incident_id,
                "",
                "## Summary",
                summary,
                "",
                "## Tests",
                "\n".join(f"- {test}" for test in bundle.tests) or "- Not provided",
                "",
                "## Gates",
                "\n".join(f"- {gate}" for gate in bundle.gates) or "- Not provided",
                "",
                "## Rollback Plan",
                bundle.rollback_plan,
                "",
                "## Evidence",
                f"- External evidence status: {bundle.external_evidence_status}",
                f"- Artifact hashes: {bundle.artifact_hashes}",
                "",
                "## Review Policy",
                f"- Allowed: {policy.allowed}",
                f"- Blockers: {', '.join(policy.blockers) if policy.blockers else 'none'}",
            ]
        )
        return PullRequestDraft(
            title=f"[agent-repair] {incident_id}: {summary[:72]}",
            body=body,
            base=base,
            head=branch,
            labels=("agent-repair", "needs-owner-review"),
            ready_for_review=policy.allowed,
        )
