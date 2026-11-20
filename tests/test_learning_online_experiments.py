"""Online learning must compare different policies, measure cost/latency/safety
per arm, keep its false-promotion rate under alpha, and roll back on harm."""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

import pytest

from omnidesk_agent.self_learning.approval import HumanApprovalGate
from omnidesk_agent.self_learning.experiments.experiment_manager import ExperimentManager
from omnidesk_agent.self_learning.experiments.sequential_gate import ArmStats, GateConfig, SequentialGate
from omnidesk_agent.self_learning.experiments.stats import binomial_sf, fisher_one_sided_greater, mcnemar_one_sided, normal_quantile
from omnidesk_agent.self_learning.observability.audit import LearningAuditLog
from omnidesk_agent.self_learning.policy_hook import RunOutcome, classify_safety_events
from omnidesk_agent.self_learning.policy_registry import PolicyRegistry
from omnidesk_agent.self_learning.rollout import RolloutController, RolloutSpec
from omnidesk_agent.self_learning.runtime_loop import RuntimeLearningLoop
from omnidesk_agent.self_learning.shadow import ShadowRunner
from omnidesk_agent.self_learning.store import SelfLearningStore


def test_exact_tests_match_known_values():
    assert binomial_sf(9, 10) == pytest.approx(11 / 1024)
    assert mcnemar_one_sided(8, 0) == pytest.approx(1 / 256)
    # Fisher one-sided: 3/3 vs 0/3 -> 1/20.
    assert fisher_one_sided_greater(3, 3, 0, 3) == pytest.approx(0.05)
    assert normal_quantile(0.975) == pytest.approx(1.959964, abs=1e-5)


def _simulate(gate, *, p_control, p_treatment, rng, per_look=100, mode="ab"):
    control, treatment = ArmStats(), ArmStats()
    for look in range(1, gate.config.max_looks + 1):
        for arm, p in ((control, p_control), (treatment, p_treatment)):
            for _ in range(per_look):
                arm.n += 1
                arm.successes += rng.random() < p
                arm.cost_n += 1
                arm.cost_sum += 0.01
        decision = gate.evaluate(control, treatment, look=look, mode=mode)
        if decision.decision != "continue":
            return decision.decision
    return "continue"


def test_aa_experiments_promote_and_roll_back_at_most_alpha_of_the_time():
    gate = SequentialGate(GateConfig(alpha=0.05, max_looks=10, min_samples_per_arm=100))
    rng = random.Random(20261121)
    outcomes = [_simulate(gate, p_control=0.7, p_treatment=0.7, rng=rng) for _ in range(400)]
    assert outcomes.count("promote_candidate") / 400 <= 0.05
    assert outcomes.count("rollback") / 400 <= 0.05


def test_a_real_improvement_is_promoted_and_a_real_regression_rolled_back():
    gate = SequentialGate(GateConfig(alpha=0.05, max_looks=10, min_samples_per_arm=100))
    rng = random.Random(7)
    better = [_simulate(gate, p_control=0.70, p_treatment=0.80, rng=rng) for _ in range(100)]
    worse = [_simulate(gate, p_control=0.80, p_treatment=0.65, rng=rng) for _ in range(100)]
    assert better.count("promote_candidate") >= 80
    assert worse.count("rollback") >= 80


def test_unmeasured_cost_blocks_promotion_instead_of_counting_as_free():
    gate = SequentialGate(GateConfig(min_samples_per_arm=10, max_looks=1))
    control = ArmStats(n=500, successes=300)
    treatment = ArmStats(n=500, successes=420)
    decision = gate.evaluate(control, treatment, look=1, mode="ab")
    assert decision.decision == "stop_inconclusive"
    assert "cost_unmeasured" in decision.checks["blockers"]


def test_safety_events_roll_back_before_the_minimum_sample_size():
    gate = SequentialGate(GateConfig(min_samples_per_arm=1000))
    decision = gate.evaluate(ArmStats(n=60, successes=40), ArmStats(n=60, successes=40, safety_runs=8), look=1, mode="canary")
    assert decision.decision == "rollback"


def test_safety_comes_from_structured_prefixes_not_the_word_security():
    results = [
        {"ok": True, "summary": "reviewed the security page"},
        {"ok": False, "error": "PermissionError: Denied by default permission policy"},
        {"ok": False, "error": "PermissionError: Denied by user"},
        {"ok": False, "error": "Execution skipped: verification missing"},
    ]
    assert classify_safety_events(results) == ("permission_denied", "approval_denied", "execution_strategy_blocked")
    assert not RunOutcome(status="failed", safety_events=("approval_denied",)).policy_safety_event


# ---------------------------------------------------------------- rollouts

@dataclass
class Msg:
    channel: str
    sender_id: str
    text: str
    thread_id: str
    message_id: str = ""


def _setup(tmp_path, *, planner_applies_policy=True, evaluated=True):
    store = SelfLearningStore(tmp_path / "self_learning.sqlite3")
    registry = PolicyRegistry(tmp_path / "learning_policy.sqlite3", approvals=HumanApprovalGate(store))
    artifact = registry.register_candidate("skill", "extract", "Use browser.extract for prices.")
    if evaluated:
        registry.record_evaluation(artifact.artifact_id, {"verdict": "improved", "method": "paired_replay_v1", "trace_count": 30})
        approval = registry.request_approval(artifact.artifact_id)
        registry.decide(approval.approval_id, "approved", reviewer="owner@example.com")
    experiments = ExperimentManager(tmp_path / "experiments.sqlite3")
    controller = RolloutController(
        registry, tmp_path / "rollouts.sqlite3", experiments=experiments,
        audit=LearningAuditLog(tmp_path / "audit.jsonl"), planner_applies_policy=planner_applies_policy,
    )
    return store, registry, artifact, controller, experiments


def _drive(loop, *, n, p_control, p_treatment, rng, cost=0.01, latency=900.0, treatment_safety=0.0):
    """A scripted world: the run outcome depends on the policy the planner got."""
    decisions = []
    for i in range(n):
        assignment = loop.assign(Msg("telegram", f"u{i % 50}", "check price", thread_id=f"t{i}"))
        treated = "browser.extract" in assignment.policy.skills_block
        p = p_treatment if treated else p_control
        success = rng.random() < p
        events = ("permission_denied",) if treated and rng.random() < treatment_safety else ()
        outcome = RunOutcome(status="completed" if success else "failed", latency_ms=latency, model_cost_usd=cost, safety_events=events)
        decision = loop.observe(assignment, outcome)
        if decision:
            decisions.append(decision)
            if decision["status"] != "running":
                break
    return decisions


def test_rollout_needs_a_policy_aware_planner_approval_and_offline_evidence(tmp_path):
    store, registry, artifact, controller, _ = _setup(tmp_path, planner_applies_policy=False)
    with pytest.raises(PermissionError, match="identical arms"):
        controller.start(artifact.artifact_id, actor="owner@example.com")
    controller.planner_applies_policy = True
    with pytest.raises(PermissionError, match="human actor"):
        controller.start(artifact.artifact_id, actor="system")
    other = registry.register_candidate("prompt", "unreviewed", "Keep answers short.")
    with pytest.raises(PermissionError, match="stored human approval"):
        controller.start(other.artifact_id, actor="owner@example.com")
    store.close()


def test_treatment_runs_get_the_candidate_policy_and_control_runs_do_not(tmp_path):
    store, registry, artifact, controller, _ = _setup(tmp_path)
    controller.start(artifact.artifact_id, actor="owner@example.com", spec=RolloutSpec(mode="ab", ab_treatment_percent=50))
    loop = RuntimeLearningLoop(rollout=controller)
    arms = {}
    for i in range(200):
        a = loop.assign(Msg("telegram", "u1", "secret text 42", thread_id=f"t{i}"))
        arms.setdefault(a.arm, set()).add(a.policy.version)
        assert "secret" not in (a.unit_hash or "")
    assert set(arms) == {"control", "treatment"}
    assert arms["treatment"] == {registry.candidate_policy(artifact.artifact_id).version}
    assert arms["control"] == {registry.active_policy().version}
    store.close()


def test_ab_rollout_promotes_a_better_candidate_and_records_online_evidence(tmp_path):
    store, registry, artifact, controller, experiments = _setup(tmp_path)
    spec = RolloutSpec(mode="ab", ab_treatment_percent=50, look_every=50, gate=GateConfig(min_samples_per_arm=150, max_looks=20))
    started = controller.start(artifact.artifact_id, actor="owner@example.com", spec=spec)
    decisions = _drive(RuntimeLearningLoop(rollout=controller), n=4000, p_control=0.6, p_treatment=0.85, rng=random.Random(1))

    final = decisions[-1]
    assert final["status"] == "ready_to_promote"
    arms = final["arms"]
    assert arms["treatment"]["cost_n"] == arms["treatment"]["n"] and arms["treatment"]["latency_n"] == arms["treatment"]["n"]
    evaluation = registry.get(artifact.artifact_id).evaluation
    assert evaluation["method"] == "online_ab_v1" and evaluation["verdict"] == "improved"
    assert len(experiments.observations(started["experiment_id"])) == arms["control"]["n"] + arms["treatment"]["n"]
    # Promotion still needs a human apply; nothing changed the active policy.
    assert registry.active_policy().is_empty
    registry.apply(artifact.artifact_id, actor="owner@example.com")
    assert "browser.extract" in registry.active_policy().skills_block
    store.close()


def test_canary_rolls_back_automatically_when_the_candidate_causes_safety_events(tmp_path):
    store, registry, artifact, controller, _ = _setup(tmp_path)
    controller.start(artifact.artifact_id, actor="owner@example.com", spec=RolloutSpec(mode="canary", stages=(10.0, 25.0), look_every=20))
    decisions = _drive(RuntimeLearningLoop(rollout=controller), n=5000, p_control=0.8, p_treatment=0.8, rng=random.Random(3), treatment_safety=0.3)

    assert decisions[-1]["status"] == "rolled_back"
    assert controller.current() is None
    assert registry.get(artifact.artifact_id).status == "rolled_back"  # needs new evaluation and approval
    assert registry.history()[0]["action"] == "experiment_rollback"
    # After the rollback every run gets the active policy again.
    assert RuntimeLearningLoop(rollout=controller).assign(Msg("c", "u", "t", thread_id="z")).arm == "none"
    store.close()


def test_canary_rolls_back_on_an_slo_breach_in_the_current_stage(tmp_path):
    store, registry, artifact, controller, _ = _setup(tmp_path)
    controller.start(artifact.artifact_id, actor="owner@example.com",
                     spec=RolloutSpec(mode="canary", stages=(20.0,), look_every=10_000, slo_p95_latency_ms=2000.0))
    loop = RuntimeLearningLoop(rollout=controller)
    rng = random.Random(5)
    result = None
    for i in range(2000):
        a = loop.assign(Msg("c", "u", "t", thread_id=f"t{i}"))
        latency = 5000.0 if a.arm == "treatment" else 800.0
        result = loop.observe(a, RunOutcome(status="completed" if rng.random() < 0.9 else "failed", latency_ms=latency, model_cost_usd=0.01))
        if result and result["status"] != "running":
            break
    assert result["status"] == "rolled_back" and "p95 latency" in result["finished_reason"]
    store.close()


def test_canary_advances_through_stages_before_it_is_ready(tmp_path):
    store, registry, artifact, controller, _ = _setup(tmp_path)
    spec = RolloutSpec(mode="canary", stages=(10.0, 30.0, 50.0), look_every=40, min_stage_samples=40,
                       gate=GateConfig(min_samples_per_arm=60, max_looks=40))
    controller.start(artifact.artifact_id, actor="owner@example.com", spec=spec)
    decisions = _drive(RuntimeLearningLoop(rollout=controller), n=20000, p_control=0.8, p_treatment=0.95, rng=random.Random(11))

    events = [e["event"] for e in controller.events(decisions[-1]["rollout_id"])]
    assert events.count("stage_advanced") == 2
    assert decisions[-1]["status"] == "ready_to_promote"
    assert registry.get(artifact.artifact_id).evaluation["method"] == "online_canary_v1"
    store.close()


def test_a_policy_change_during_the_rollout_stops_it(tmp_path):
    store, registry, artifact, controller, _ = _setup(tmp_path)
    controller.start(artifact.artifact_id, actor="owner@example.com")
    other = registry.register_candidate("workflow", "w", "Check stock before quoting.")
    registry.record_evaluation(other.artifact_id, {"verdict": "improved", "method": "paired_replay_v1", "trace_count": 3})
    approval = registry.request_approval(other.artifact_id)
    registry.decide(approval.approval_id, "approved", reviewer="owner@example.com")
    registry.apply(other.artifact_id, actor="owner@example.com")

    assignment = controller.assign(Msg("c", "u", "t", thread_id="x"))
    assert assignment.arm == "none" and controller.current() is None
    store.close()


def test_without_a_rollout_the_hook_records_nothing(tmp_path):
    experiments = ExperimentManager(tmp_path / "experiments.sqlite3")
    loop = RuntimeLearningLoop(experiments)
    legacy = loop.assign_policy(Msg("telegram", "u1", "hello", thread_id="t1"))
    assert legacy["arm"] == "none" and "assignment_token" not in legacy
    assert loop.observe_result(legacy, status="completed", result_count=1, safety_violation=True) is None
    assert experiments.get("planner_policy_online_v1") is None  # no default A/A experiment any more


# ------------------------------------------------------------------ shadow

def test_shadow_plans_with_the_candidate_but_never_executes(tmp_path):
    calls = []

    async def plan_only(msg, policy):
        calls.append(policy.version)
        return {"steps": [{"tool": "browser", "action": "extract", "args": {"selector": "#price"}, "risk": "low"}]}

    store, registry, artifact, _, _ = _setup(tmp_path)
    shadow = ShadowRunner(tmp_path / "shadow.sqlite3", plan_only, sample_rate=1.0, rng=random.Random(0))
    candidate = registry.candidate_policy(artifact.artifact_id)
    shadow.set_candidate(candidate)
    production = {"steps": [{"tool": "browser", "action": "click", "args": {"selector": "#price"}, "risk": "low"}]}

    async def scenario():
        loop = RuntimeLearningLoop(shadow=shadow)
        loop.after_plan(Msg("c", "u", "private text", thread_id="t"), None, production, run_key="run-1")
        await shadow.drain()

    asyncio.run(scenario())
    assert calls == [candidate.version]
    join = shadow.observe_production("run-1", RunOutcome(status="failed"), failed_step_index=0)
    assert join == "diverged_before_failure"
    report = shadow.report(candidate.version)
    assert report["shadow_runs"] == 1 and report["identical_rate"] == 0.0
    assert "private text" not in (tmp_path / "shadow.sqlite3").read_bytes().decode("latin-1")
    store.close()


def test_shadow_respects_sampling_timeout_and_errors(tmp_path):
    async def slow(msg, policy):
        await asyncio.sleep(5)

    async def broken(msg, policy):
        raise PermissionError("prompt injection")

    store, registry, artifact, _, _ = _setup(tmp_path)
    candidate = registry.candidate_policy(artifact.artifact_id)

    async def scenario():
        never = ShadowRunner(tmp_path / "a.sqlite3", slow, sample_rate=0.0)
        never.set_candidate(candidate)
        assert never.after_plan(Msg("c", "u", "t", "t"), {"steps": []}, run_key="r0") is None
        timed = ShadowRunner(tmp_path / "b.sqlite3", slow, sample_rate=1.0, timeout_seconds=0.05)
        timed.set_candidate(candidate)
        timed.after_plan(Msg("c", "u", "t", "t"), {"steps": []}, run_key="r1")
        failing = ShadowRunner(tmp_path / "c.sqlite3", broken, sample_rate=1.0)
        failing.set_candidate(candidate)
        failing.after_plan(Msg("c", "u", "t", "t"), {"steps": []}, run_key="r2")
        await timed.drain()
        await failing.drain()
        return timed.report(candidate.version), failing.report(candidate.version)

    timed_report, failing_report = asyncio.run(scenario())
    assert timed_report["statuses"] == {"timeout": 1}
    assert failing_report["statuses"] == {"planner_error": 1}
    store.close()


# ------------------------------------------------ review findings on #229

def test_rollout_refuses_an_evaluation_made_against_another_active_policy(tmp_path):
    store, registry, artifact, controller, _ = _setup(tmp_path)
    other = registry.register_candidate("workflow", "other", "Check stock before quoting.")
    registry.record_evaluation(other.artifact_id, {"verdict": "improved", "method": "paired_replay_v1", "trace_count": 3})
    approval = registry.request_approval(other.artifact_id)
    registry.decide(approval.approval_id, "approved", reviewer="owner@example.com")
    registry.apply(other.artifact_id, actor="owner@example.com")  # active policy changed after artifact's replay
    with pytest.raises(PermissionError, match="another active policy"):
        controller.start(artifact.artifact_id, actor="owner@example.com")
    store.close()


def test_perfect_samples_do_not_prove_non_inferiority():
    gate = SequentialGate(GateConfig(min_samples_per_arm=100, max_looks=10, require_cost_measurement=False))
    decision = gate.evaluate(ArmStats(n=2000, successes=2000), ArmStats(n=100, successes=100), look=1, mode="canary")
    assert decision.decision == "continue" and "success_noninferiority_not_shown" in decision.checks["blockers"]


def test_a_canary_worse_than_the_margin_is_rarely_promoted():
    # Control is perfect, treatment fails 2.1% of the time (worse than the 2-point margin).
    gate = SequentialGate(GateConfig(min_samples_per_arm=100, max_looks=10, require_cost_measurement=False))
    rng = random.Random(229)
    promoted = 0
    for _ in range(300):
        control, treatment = ArmStats(), ArmStats()
        for look in range(1, 11):
            control.n += 100
            control.successes += 100
            for _ in range(100):
                treatment.n += 1
                treatment.successes += rng.random() >= 0.021
            decision = gate.evaluate(control, treatment, look=look, mode="canary")
            if decision.decision != "continue":
                promoted += decision.decision == "promote_candidate"
                break
    assert promoted / 300 <= 0.05


def test_the_last_canary_stage_needs_its_minimum_samples(tmp_path):
    store, registry, artifact, controller, _ = _setup(tmp_path)
    spec = RolloutSpec(mode="canary", stages=(50.0,), look_every=50, min_stage_samples=1000, gate=GateConfig(min_samples_per_arm=50, max_looks=100))
    controller.start(artifact.artifact_id, actor="owner@example.com", spec=spec)
    loop = RuntimeLearningLoop(rollout=controller)
    rng = random.Random(1)
    for i in range(1200):
        a = loop.assign(Msg("c", "u", "t", thread_id=f"t{i}"))
        loop.observe(a, RunOutcome(status="completed" if rng.random() < 0.99 else "failed", latency_ms=100.0, model_cost_usd=0.01))
    state = controller.current()
    assert state is not None and state["status"] == "running"  # < 1000 treatment runs in the stage so far
    assert state["stage"]["n"] < 1000
    store.close()


def test_latency_slo_uses_the_most_recent_runs(tmp_path):
    store, registry, artifact, controller, _ = _setup(tmp_path)
    controller.start(artifact.artifact_id, actor="owner@example.com",
                     spec=RolloutSpec(mode="canary", stages=(50.0,), look_every=100_000, slo_p95_latency_ms=500.0))
    loop = RuntimeLearningLoop(rollout=controller)
    treated = 0
    result = None
    i = 0
    while treated < 2400:
        a = loop.assign(Msg("c", "u", "t", thread_id=f"t{i}"))
        i += 1
        latency = 100.0 if treated < 2000 else 10_000.0
        if a.arm == "treatment":
            treated += 1
        result = loop.observe(a, RunOutcome(status="completed", latency_ms=latency if a.arm == "treatment" else 100.0, model_cost_usd=0.01))
        if result and result["status"] != "running":
            break
    assert result and result["status"] == "rolled_back" and "p95 latency" in result["finished_reason"]
    store.close()


def test_production_outcome_that_arrives_first_is_joined_later(tmp_path):
    release = asyncio.Event()

    async def slow_plan(msg, policy):
        await release.wait()
        return {"steps": [{"tool": "browser", "action": "extract", "args": {}}]}

    store, registry, artifact, _, _ = _setup(tmp_path)
    shadow = ShadowRunner(tmp_path / "shadow.sqlite3", slow_plan, sample_rate=1.0)
    candidate = registry.candidate_policy(artifact.artifact_id)
    shadow.set_candidate(candidate)

    async def scenario():
        shadow.after_plan(Msg("c", "u", "t", "t"), {"steps": [{"tool": "browser", "action": "click", "args": {}}]}, run_key="r1")
        early = shadow.observe_production("r1", RunOutcome(status="failed"), failed_step_index=0)
        release.set()
        await shadow.drain()
        return early

    assert asyncio.run(scenario()) is None  # shadow still planning
    assert shadow.report(candidate.version)["joins"] == {"diverged_before_failure": 1}
    store.close()
