"""Paired replay of recorded runs under a baseline and a candidate policy.

Each recorded request is planned twice by the same planner (scripted model in
CI, optionally a live model), once with the baseline policy and once with the
candidate. Neither plan is executed. A plan's outcome comes from an oracle that
only knows recorded tool outcomes: a step whose tool, action and arguments
were recorded gets the recorded result; any other step is ``unknown``. A plan
with an unknown step before its first known failure is ``unknown`` and is not
credited. Only cases where both arms are known are compared, with an exact
one-sided McNemar test on the discordant pairs.

This measures "does the candidate steer the planner towards steps that are
known to work, and away from steps known to fail". It cannot credit new steps
it has never seen succeed; the report says how many cases were unverifiable.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Literal, Optional, Protocol

from omnidesk_agent.self_learning.experiments.stats import mcnemar_one_sided
from omnidesk_agent.self_learning.policy_hook import PlannerPolicy

Outcome = Literal["ok", "fail", "unknown"]
PlanFn = Callable[[dict[str, Any], PlannerPolicy], Awaitable[Any]]

# Arguments the orchestrator or planner fill in from descriptions; they say
# nothing about what the tool does.
VOLATILE_ARGS = frozenset({"expected_result", "verification", "retry_policy", "auto_ground", "auto_click_grounded"})
METHOD = "paired_replay_v1"


def args_digest(args: Optional[dict[str, Any]]) -> str:
    functional = {k: v for k, v in (args or {}).items() if k not in VOLATILE_ARGS}
    canonical = json.dumps(functional, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class RecordedStep:
    tool: str
    action: str
    digest: str
    ok: bool


@dataclass(frozen=True)
class ReplayCase:
    case_id: str
    message: dict[str, Any]
    steps: tuple[RecordedStep, ...]
    status: str


class OutcomeOracle(Protocol):
    name: str

    def outcome(self, tool: str, action: str, digest: str) -> Outcome: ...


class RecordedOutcomeOracle:
    """Outcomes from recorded runs; conflicting records (flaky steps) are unknown."""

    name = "recorded_outcomes_v1"

    def __init__(self, cases: Iterable[ReplayCase]):
        seen: dict[tuple[str, str, str], set[bool]] = defaultdict(set)
        for case in cases:
            for step in case.steps:
                seen[(step.tool, step.action, step.digest)].add(step.ok)
        self._seen = dict(seen)

    def outcome(self, tool: str, action: str, digest: str) -> Outcome:
        results = self._seen.get((tool, action, digest))
        if not results or len(results) > 1:
            return "unknown"
        return "ok" if True in results else "fail"


def cases_from_runs(runs: Iterable[dict[str, Any]]) -> tuple[list[ReplayCase], dict[str, int]]:
    """Build cases from RunStore rows (``RunStore.get`` shape).

    A run is usable only when its plan was stored and every result maps to one
    plan step (runs with interleaved vision results are skipped).
    """
    cases: list[ReplayCase] = []
    skipped = {"no_plan": 0, "unmapped_results": 0, "not_final": 0}
    for run in runs:
        status = str(run.get("status") or "")
        if status not in {"completed", "failed"}:
            skipped["not_final"] += 1
            continue
        plan = run.get("plan_json") or {}
        steps = plan.get("steps") if isinstance(plan, dict) else None
        results = run.get("results") or []
        if not steps:
            skipped["no_plan"] += 1
            continue
        if len(results) > len(steps) or (status == "completed" and len(results) != len(steps)):
            skipped["unmapped_results"] += 1
            continue
        recorded = tuple(
            RecordedStep(str(step.get("tool")), str(step.get("action")), args_digest(step.get("args")), bool(result.get("ok")))
            for step, result in zip(steps, results)
        )
        message = run.get("original_message") or {}
        cases.append(ReplayCase(case_id=str(run.get("id")), message=dict(message), steps=recorded, status=status))
    return cases, skipped


@dataclass
class ReplayReport:
    method: str
    oracle: str
    verdict: str
    reason: str
    case_count: int
    trace_count: int  # comparable cases
    wins: int
    losses: int
    ties: int
    baseline_unknown: int
    candidate_unknown: int
    candidate_plan_errors: int
    excluded_both_errored: int
    p_improved: float
    p_regressed: float
    alpha: float
    baseline_version: str
    candidate_version: str
    policy_improvement_score: Optional[float] = None
    learning_regression_score: Optional[float] = None
    skipped: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReplayEngine:
    def __init__(
        self,
        plan_fn: PlanFn,
        *,
        alpha: float = 0.05,
        min_comparable: int = 10,
        max_unknown_fraction: float = 0.5,
        plan_timeout_seconds: float = 60.0,
    ):
        self.plan_fn = plan_fn
        self.alpha = float(alpha)
        self.min_comparable = int(min_comparable)
        self.max_unknown_fraction = float(max_unknown_fraction)
        self.plan_timeout_seconds = float(plan_timeout_seconds)

    async def evaluate(
        self,
        cases: list[ReplayCase],
        *,
        baseline: PlannerPolicy,
        candidate: PlannerPolicy,
        oracle: Optional[OutcomeOracle] = None,
        skipped: Optional[dict[str, int]] = None,
    ) -> ReplayReport:
        oracle = oracle or RecordedOutcomeOracle(cases)
        wins = losses = ties = base_unknown = cand_unknown = cand_errors = both_errored = 0
        for case in cases:
            base, base_err = await self._outcome(case, baseline, oracle)
            cand, cand_err = await self._outcome(case, candidate, oracle)
            if base_err and cand_err:
                both_errored += 1  # e.g. an injected request both planners refuse
                continue
            if cand_err:
                cand_errors += 1
            if base == "unknown":
                base_unknown += 1
            if cand == "unknown":
                cand_unknown += 1
            if "unknown" in (base, cand):
                continue
            if cand == "ok" and base == "fail":
                wins += 1
            elif cand == "fail" and base == "ok":
                losses += 1
            else:
                ties += 1
        comparable = wins + losses + ties
        p_improved = mcnemar_one_sided(wins, losses)
        p_regressed = mcnemar_one_sided(losses, wins)
        scored = len(cases) - both_errored
        verdict, reason = self._verdict(comparable, scored, cand_unknown, wins, losses, p_improved, p_regressed)
        return ReplayReport(
            method=METHOD,
            oracle=getattr(oracle, "name", type(oracle).__name__),
            verdict=verdict,
            reason=reason,
            case_count=len(cases),
            trace_count=comparable,
            wins=wins,
            losses=losses,
            ties=ties,
            baseline_unknown=base_unknown,
            candidate_unknown=cand_unknown,
            candidate_plan_errors=cand_errors,
            excluded_both_errored=both_errored,
            p_improved=round(p_improved, 6),
            p_regressed=round(p_regressed, 6),
            alpha=self.alpha,
            baseline_version=baseline.version,
            candidate_version=candidate.version,
            policy_improvement_score=round(wins / comparable, 6) if comparable else None,
            learning_regression_score=round(losses / comparable, 6) if comparable else None,
            skipped=dict(skipped or {}),
        )

    def _verdict(self, comparable: int, scored: int, cand_unknown: int, wins: int, losses: int, p_improved: float, p_regressed: float) -> tuple[str, str]:
        if p_regressed < self.alpha:
            return "regressed", "candidate lost significantly more verifiable cases than it won"
        if comparable < self.min_comparable:
            return "inconclusive", f"only {comparable} verifiable cases (need {self.min_comparable})"
        if scored and cand_unknown / scored > self.max_unknown_fraction:
            return "inconclusive", "most candidate plans contain steps with no recorded outcome"
        if p_improved < self.alpha:
            return "improved", "candidate won significantly more verifiable cases than it lost"
        if losses == 0:
            return "non_inferior", "candidate lost no verifiable case"
        return "inconclusive", "difference is not significant"

    async def _outcome(self, case: ReplayCase, policy: PlannerPolicy, oracle: OutcomeOracle) -> tuple[Outcome, bool]:
        try:
            plan = await asyncio.wait_for(self.plan_fn(case.message, policy), timeout=self.plan_timeout_seconds)
        except Exception:
            return "fail", True  # a planner that errors does not complete the task
        steps = list(getattr(plan, "steps", None) or (plan.get("steps") if isinstance(plan, dict) else []) or [])
        if not steps:
            return "fail", False
        for step in steps:
            tool = str(_field(step, "tool"))
            action = str(_field(step, "action"))
            result = oracle.outcome(tool, action, args_digest(_field(step, "args") or {}))
            if result != "ok":
                return result, False
        return "ok", False


def _field(step: Any, name: str) -> Any:
    return step.get(name) if isinstance(step, dict) else getattr(step, name, None)
