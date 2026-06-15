from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from omnidesk_agent.self_healing import RuntimeSelfHealingController, RuntimeSignal


@dataclass(frozen=True)
class IncidentReview:
    incident_id: str
    finding_type: str
    root_cause_confidence: float
    readonly: bool = True
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RepairProposal:
    incident_id: str
    proposed_action: str
    files_to_change: tuple[str, ...]
    requires_human_approval: bool
    rollback_plan: str
    risk_level: Literal["low", "medium", "high", "critical"]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GateResult:
    incident_id: str
    passed: bool
    gates: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IncidentReviewer:
    def review(self, signal: RuntimeSignal) -> IncidentReview:
        finding = "runtime_failure"
        if signal.component.startswith("push"):
            finding = "push_delivery_failure"
        elif signal.component.startswith("postgres"):
            finding = "postgres_soak_failure"
        elif signal.safety_violation:
            finding = "safety_violation"
        confidence = 0.82 if signal.consecutive_failures >= 3 or signal.error_rate >= 0.20 else 0.55
        return IncidentReview(
            incident_id=f"inc-{int(time.time())}-{abs(hash(signal.component)) % 100000}",
            finding_type=finding,
            root_cause_confidence=confidence,
            evidence={
                "component": signal.component,
                "error_rate": signal.error_rate,
                "consecutive_failures": signal.consecutive_failures,
                "health_ok": signal.health_ok,
            },
        )


class RepairPlanner:
    def __init__(self, controller: RuntimeSelfHealingController | None = None):
        self.controller = controller or RuntimeSelfHealingController()

    def plan(self, signal: RuntimeSignal, review: IncidentReview) -> RepairProposal:
        decision = self.controller.decide(signal)
        risk = "high" if decision.requires_human_approval else "medium"
        files: tuple[str, ...] = ()
        if decision.action == "create_upgrade_proposal":
            files = ("tests/regression/<incident>.py",)
        return RepairProposal(
            incident_id=review.incident_id,
            proposed_action=decision.action,
            files_to_change=files,
            requires_human_approval=decision.requires_human_approval,
            rollback_plan=decision.rollback_ref or "restore previous signed release artifact or disable generated branch",
            risk_level=risk,
        )


class GateRunner:
    def required_gates(self, proposal: RepairProposal) -> tuple[str, ...]:
        base = ["unit_tests", "ga_release_gate", "external_evidence_gate_audit_only"]
        if proposal.files_to_change:
            base.append("diff_check")
        if proposal.risk_level in {"high", "critical"}:
            base.append("owner_approval")
        return tuple(base)

    def evaluate(self, proposal: RepairProposal, *, passed_gates: tuple[str, ...]) -> GateResult:
        required = set(self.required_gates(proposal))
        passed = set(passed_gates)
        missing = sorted(required - passed)
        return GateResult(
            incident_id=proposal.incident_id,
            passed=not missing,
            gates=tuple(sorted(required)),
            reason="passed" if not missing else f"missing gates: {', '.join(missing)}",
        )


def build_iterative_repair_record(signal: RuntimeSignal) -> dict[str, Any]:
    reviewer = IncidentReviewer()
    planner = RepairPlanner()
    gates = GateRunner()
    review = reviewer.review(signal)
    proposal = planner.plan(signal, review)
    gate_result = gates.evaluate(proposal, passed_gates=("unit_tests",))
    return {
        "review": review.to_dict(),
        "proposal": proposal.to_dict(),
        "gate": gate_result.to_dict(),
        "promotion": {
            "mode": "pr_only",
            "canary_required": proposal.risk_level in {"high", "critical"},
            "human_approval_required": proposal.requires_human_approval,
        },
    }
