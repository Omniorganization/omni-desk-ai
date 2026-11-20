"""Build the learning runtime the daemon passes to the orchestrator."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnidesk_agent.self_learning.approval import HumanApprovalGate
from omnidesk_agent.self_learning.core_hook import CoreExperimentHook
from omnidesk_agent.self_learning.observability.audit import LearningAuditLog
from omnidesk_agent.self_learning.policy_registry import PolicyRegistry
from omnidesk_agent.self_learning.rollout import RolloutController
from omnidesk_agent.self_learning.runtime_loop import RuntimeLearningLoop
from omnidesk_agent.self_learning.store import SelfLearningStore


@dataclass
class LearningRuntime:
    hook: CoreExperimentHook
    loop: RuntimeLearningLoop
    registry: PolicyRegistry
    rollout: RolloutController
    store: SelfLearningStore

    def close(self) -> None:
        self.store.close()


def build_learning_runtime(workspace_root: Path, experiments: Any) -> LearningRuntime:
    """Active learned policy for every run, plus any operator-started rollout.

    The orchestrator applies policies (agent-core's hook protocol), so
    rollouts are allowed; none runs until an operator starts one.
    """
    root = Path(workspace_root).expanduser()
    store = SelfLearningStore(root / "self_learning.sqlite3")
    audit = LearningAuditLog(root / "learning_audit.jsonl")
    registry = PolicyRegistry(root / "learning_policy.sqlite3", approvals=HumanApprovalGate(store), audit=audit)
    rollout = RolloutController(registry, root / "learning_rollouts.sqlite3", experiments=experiments, audit=audit, planner_applies_policy=True)
    loop = RuntimeLearningLoop(experiments, rollout=rollout, registry=registry)
    return LearningRuntime(hook=CoreExperimentHook(loop), loop=loop, registry=registry, rollout=rollout, store=store)
