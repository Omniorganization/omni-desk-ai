"""End to end through agent-core's real Orchestrator: the treatment arm's planner
gets the candidate policy, outcomes flow back to the rollout gate, and a
better candidate becomes ready to promote while a harmful one rolls back."""
from __future__ import annotations

import asyncio
import random

from omnidesk_agent.config import PermissionConfig
from omnidesk_agent.core.agent_loop import TURN_CONTINUE, TURN_DONE, TURN_GIVE_UP, PlannerTurn
from omnidesk_agent.core.models import ChannelMessage, PlanStep, ToolResult
from omnidesk_agent.core.orchestrator import Orchestrator
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.self_learning.approval import HumanApprovalGate
from omnidesk_agent.self_learning.core_hook import CoreExperimentHook, to_core_policy
from omnidesk_agent.self_learning.experiments.sequential_gate import GateConfig
from omnidesk_agent.self_learning.policy_registry import PolicyRegistry
from omnidesk_agent.self_learning.rollout import RolloutController, RolloutSpec
from omnidesk_agent.self_learning.runtime_loop import RuntimeLearningLoop
from omnidesk_agent.self_learning.shadow import ShadowRunner
from omnidesk_agent.self_learning.store import SelfLearningStore
from omnidesk_agent.tools.registry import ToolRegistry


class PriceTool:
    """extract works; click fails 60% of the time (a flaky selector)."""

    name = "shop"

    def __init__(self, rng):
        self.rng = rng
        self.calls = []

    async def call(self, action, args, ctx):
        self.calls.append(action)
        if action == "wipe":
            raise PermissionError("Denied by default permission policy")
        if action == "extract":
            return ToolResult(True, data={"price": 10}, summary="price 10")
        ok = self.rng.random() < 0.4
        return ToolResult(ok, data={}, summary="clicked" if ok else None, error=None if ok else "selector not found")


class PolicyFollowingPlanner:
    """Stands in for the LLM: uses `shop.extract` only when the policy carries that skill."""

    def __init__(self):
        self.policies = []

    async def next_turn(self, view):
        self.policies.append(view.policy.version if view.policy else None)
        if view.turn_index > 1:
            last = [step for turn in view.history for step in turn.get("steps", [])][-1:]
            if last and not last[0].get("ok"):  # the step failed: this planner gives up instead of retrying
                return PlannerTurn(TURN_GIVE_UP, "read the price", [], final_answer="could not read the price")
            return PlannerTurn(TURN_DONE, "read the price", [], final_answer="done")
        skills = view.policy.skills_block if view.policy else ""
        action = "wipe" if "shop.wipe" in skills else ("extract" if "shop.extract" in skills else "click")
        step = PlanStep(description="read price", tool="shop", action=action, args={"selector": "#price"}, risk="low", requires_approval=False)
        return PlannerTurn(TURN_CONTINUE, "read the price", [step])


def _runtime(tmp_path, *, body, rng):
    store = SelfLearningStore(tmp_path / "self_learning.sqlite3")
    registry = PolicyRegistry(tmp_path / "learning_policy.sqlite3", approvals=HumanApprovalGate(store))
    artifact = registry.register_candidate("skill", "price", body)
    registry.record_evaluation(artifact.artifact_id, {"verdict": "improved", "method": "paired_replay_v1", "trace_count": 20})
    approval = registry.request_approval(artifact.artifact_id)
    registry.decide(approval.approval_id, "approved", reviewer="owner@example.com")
    rollout = RolloutController(registry, tmp_path / "rollouts.sqlite3", planner_applies_policy=True)
    planner = PolicyFollowingPlanner()
    tools = ToolRegistry()
    tool = PriceTool(rng)
    tools.register(tool)
    permissions = PermissionManager(PermissionConfig(default_mode="allow", audit_log=tmp_path / "audit.jsonl"))
    orchestrator = Orchestrator(planner, tools, permissions, ExperienceStore(tmp_path / "memory.sqlite3"))
    return store, registry, artifact, rollout, planner, orchestrator, tool


def _drive(orchestrator, n):
    async def run():
        results = []
        for i in range(n):
            results.append(await orchestrator.handle_message(ChannelMessage(channel="telegram", sender_id=f"u{i % 30}", text="price?", thread_id=f"t{i}")))
        return results
    return asyncio.run(run())


def test_a_better_candidate_is_promoted_through_the_real_orchestrator(tmp_path):
    store, registry, artifact, rollout, planner, orchestrator, tool = _runtime(tmp_path, body="Use shop.extract to read prices.", rng=random.Random(4))
    orchestrator.learning_loop = CoreExperimentHook(RuntimeLearningLoop(rollout=rollout))
    started = rollout.start(artifact.artifact_id, actor="owner@example.com", spec=RolloutSpec(
        mode="ab", ab_treatment_percent=50, look_every=25,
        gate=GateConfig(min_samples_per_arm=40, max_looks=20, require_cost_measurement=False)))
    _drive(orchestrator, 600)

    candidate = registry.candidate_policy(artifact.artifact_id).version
    assert candidate in planner.policies and None in planner.policies  # both arms really ran
    assert "extract" in tool.calls and "click" in tool.calls
    assert rollout.get(started["rollout_id"])["status"] == "ready_to_promote"
    evaluation = registry.get(artifact.artifact_id).evaluation
    assert evaluation["method"] == "online_ab_v1" and evaluation["verdict"] == "improved"
    arms = rollout.get(started["rollout_id"])["arms"]
    assert arms["treatment"]["latency_n"] == arms["treatment"]["n"] > 0  # latency came from the real loop
    store.close()


def test_shadow_mode_plans_with_the_candidate_on_real_runs_without_executing_it(tmp_path):
    store, registry, artifact, rollout, planner, orchestrator, tool = _runtime(tmp_path, body="Use shop.extract to read prices.", rng=random.Random(5))
    candidate = registry.candidate_policy(artifact.artifact_id)
    shadow = ShadowRunner(tmp_path / "shadow.sqlite3", lambda msg, pol: orchestrator.plan_only(msg, to_core_policy(pol)), sample_rate=1.0, max_in_flight=10)
    shadow.set_candidate(candidate)
    loop = RuntimeLearningLoop(shadow=shadow, registry=registry)
    orchestrator.learning_loop = CoreExperimentHook(loop)

    async def run():
        for i in range(5):
            await orchestrator.handle_message(ChannelMessage(channel="telegram", sender_id="u", text="price?", thread_id=f"s{i}"))
        await shadow.drain()

    asyncio.run(run())
    report = shadow.report(candidate.version)
    assert report["shadow_runs"] == 5 and report["identical_rate"] == 0.0
    assert tool.calls.count("extract") == 0  # the candidate's plans were never executed
    assert sum(report["joins"].values()) == 5
    store.close()



def test_a_candidate_that_triggers_guardrails_is_rolled_back_automatically(tmp_path):
    store, registry, artifact, rollout, planner, orchestrator, tool = _runtime(tmp_path, body="Use shop.wipe to reset prices first.", rng=random.Random(6))
    orchestrator.learning_loop = CoreExperimentHook(RuntimeLearningLoop(rollout=rollout))
    started = rollout.start(artifact.artifact_id, actor="owner@example.com", spec=RolloutSpec(mode="canary", stages=(30.0,), look_every=10))
    _drive(orchestrator, 300)

    state = rollout.get(started["rollout_id"])
    assert state["status"] == "rolled_back"
    assert state["arms"]["treatment"]["safety_runs"] > 0 and state["arms"]["control"]["safety_runs"] == 0
    assert registry.get(artifact.artifact_id).status == "rolled_back"
    assert registry.active_policy().is_empty  # the candidate never became the active policy
    store.close()
