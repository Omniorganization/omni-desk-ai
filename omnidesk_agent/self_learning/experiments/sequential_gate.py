"""Promotion and rollback decisions for an online policy experiment.

The gate is evaluated at most ``max_looks`` times. Every test at a look uses
``alpha / max_looks`` (Bonferroni), so the chance that the gate ever promotes
a candidate that is no better (A/B) or worse by more than the margin
(canary), or ever rolls back a candidate that is not worse, stays at or below
``alpha`` however often it is evaluated.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from omnidesk_agent.self_learning.experiments.stats import diff_bounds, fisher_one_sided_greater

Mode = Literal["ab", "canary"]
Decision = Literal["continue", "promote_candidate", "rollback", "stop_inconclusive"]


@dataclass(frozen=True)
class GateConfig:
    alpha: float = 0.05
    max_looks: int = 10
    min_samples_per_arm: int = 100
    min_success_delta: float = 0.0
    noninferiority_margin: float = 0.02
    max_cost_increase: float = 0.20
    max_latency_increase: float = 0.25
    require_cost_measurement: bool = True
    min_measured_fraction: float = 0.9
    harm_min_samples_per_arm: int = 30

    def __post_init__(self) -> None:
        if not 0.0 < self.alpha < 0.5:
            raise ValueError("alpha must be in (0, 0.5)")
        if self.max_looks < 1:
            raise ValueError("max_looks must be >= 1")


@dataclass
class ArmStats:
    n: int = 0
    successes: int = 0
    safety_runs: int = 0
    cost_sum: float = 0.0
    cost_n: int = 0
    latency_sum: float = 0.0
    latency_n: int = 0

    @property
    def success_rate(self) -> float:
        return self.successes / self.n if self.n else 0.0

    @property
    def safety_rate(self) -> float:
        return self.safety_runs / self.n if self.n else 0.0

    @property
    def mean_cost(self) -> float | None:
        return self.cost_sum / self.cost_n if self.cost_n else None

    @property
    def mean_latency(self) -> float | None:
        return self.latency_sum / self.latency_n if self.latency_n else None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(success_rate=round(self.success_rate, 6), safety_rate=round(self.safety_rate, 6),
                       mean_cost=self.mean_cost, mean_latency=self.mean_latency)
        return payload


@dataclass(frozen=True)
class GateDecision:
    decision: Decision
    reason: str
    look: int
    alpha_per_look: float
    control: dict[str, Any]
    treatment: dict[str, Any]
    checks: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SequentialGate:
    def __init__(self, config: GateConfig | None = None):
        self.config = config or GateConfig()

    def evaluate(self, control: ArmStats, treatment: ArmStats, *, look: int, mode: Mode) -> GateDecision:
        cfg = self.config
        alpha = cfg.alpha / cfg.max_looks
        checks: dict[str, Any] = {}

        def out(decision: Decision, reason: str) -> GateDecision:
            return GateDecision(decision, reason, look, alpha, control.to_dict(), treatment.to_dict(), checks)

        # Harm first: these run at every look, before any minimum sample size.
        checks["safety_harm_p"] = fisher_one_sided_greater(treatment.safety_runs, treatment.n, control.safety_runs, control.n)
        if checks["safety_harm_p"] < alpha:
            return out("rollback", "treatment has significantly more runs with policy safety events")
        lower, upper = diff_bounds(treatment.successes, treatment.n, control.successes, control.n, alpha)
        checks["success_diff_bounds"] = [round(lower, 6), round(upper, 6)]
        if min(control.n, treatment.n) >= cfg.harm_min_samples_per_arm and upper < 0:
            return out("rollback", "treatment success rate is significantly lower")

        if min(control.n, treatment.n) < cfg.min_samples_per_arm:
            return out("continue" if look < cfg.max_looks else "stop_inconclusive", "minimum sample size not reached")

        blockers: list[str] = []
        if mode == "ab":
            if lower <= cfg.min_success_delta:
                blockers.append("success_not_significantly_better")
        elif lower <= -cfg.noninferiority_margin:
            blockers.append("success_noninferiority_not_shown")
        if treatment.safety_rate > control.safety_rate:
            blockers.append("safety_rate_higher")

        measured = min(control.cost_n / max(control.n, 1), treatment.cost_n / max(treatment.n, 1))
        checks["cost_measured_fraction"] = round(measured, 6)
        if measured < cfg.min_measured_fraction:
            if cfg.require_cost_measurement:
                blockers.append("cost_unmeasured")
        elif control.mean_cost is not None and treatment.mean_cost is not None:
            increase = (treatment.mean_cost - control.mean_cost) / max(control.mean_cost, 1e-9)
            checks["cost_increase"] = round(increase, 6)
            if increase > cfg.max_cost_increase:
                blockers.append("cost_increase")
        if control.mean_latency is not None and treatment.mean_latency is not None:
            latency_increase = (treatment.mean_latency - control.mean_latency) / max(control.mean_latency, 1e-9)
            checks["latency_increase"] = round(latency_increase, 6)
            if latency_increase > cfg.max_latency_increase:
                blockers.append("latency_increase")
        checks["blockers"] = blockers

        if not blockers:
            return out("promote_candidate", "candidate passed the gate; a human applies it")
        if look >= cfg.max_looks:
            return out("stop_inconclusive", "maximum number of looks reached")
        return out("continue", ", ".join(blockers))
