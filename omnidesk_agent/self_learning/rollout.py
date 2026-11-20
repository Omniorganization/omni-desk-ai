"""Online A/B and canary rollout of one learned artefact, with automatic rollback.

A rollout exposes a share of real runs to ``active policy + candidate`` while
the rest keep the active policy. Every run's outcome (success, cost, latency,
policy safety events) is added to per-arm aggregates. Every ``look_every``
treatment observations the sequential gate decides:

- ``rollback`` (automatic): the rollout stops, all traffic returns to the
  active policy and the artefact needs a new evaluation and approval;
- ``promote_candidate``: canary mode moves to the next traffic stage; at the
  last stage (or in A/B mode) the rollout is ``ready_to_promote`` and the
  online result is recorded as the artefact's evaluation. A human applies it;
- ``stop_inconclusive``: the rollout stops without a decision.

A canary also rolls back at once when the treatment arm breaches its SLO
(error rate, p95 latency) in the current stage. Starting a rollout needs a
stored human approval of the artefact, a passing offline evaluation, and a
planner that applies policies (otherwise the experiment would be A/A).

State is one SQLite file per runtime; several gateway instances would need a
shared store.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from omnidesk_agent.self_learning.approval import is_system_identity
from omnidesk_agent.self_learning.experiments.cohort_assignment import CohortAssigner
from omnidesk_agent.self_learning.experiments.metric_collector import ExperimentObservation
from omnidesk_agent.self_learning.experiments.sequential_gate import ArmStats, GateConfig, SequentialGate
from omnidesk_agent.self_learning.observability.audit import LearningAuditLog
from omnidesk_agent.self_learning.observability.schema import LearningEvent
from omnidesk_agent.self_learning.policy_hook import PolicyAssignment, RunOutcome
from omnidesk_agent.self_learning.policy_registry import APPROVAL_TYPES, PASSING_VERDICTS, PolicyRegistry
from omnidesk_agent.self_learning.schemas import ApprovalRecord
from omnidesk_agent.storage.sqlite import connect_sqlite

Mode = Literal["ab", "canary"]
GUARD_ACTOR = "omnidesk-rollout-guard"
MAX_STAGE_LATENCIES = 2000


@dataclass(frozen=True)
class RolloutSpec:
    mode: Mode = "canary"
    ab_treatment_percent: float = 10.0
    stages: tuple[float, ...] = (1.0, 5.0, 10.0, 25.0, 50.0)
    look_every: int = 50
    min_stage_samples: int = 50
    slo_max_error_rate: float = 0.20
    slo_p95_latency_ms: Optional[float] = None
    gate: GateConfig = field(default_factory=GateConfig)

    def __post_init__(self) -> None:
        if self.mode not in ("ab", "canary"):
            raise ValueError("mode must be ab or canary")
        if not self.stages or any(not 0 < s <= 50 for s in self.stages) or list(self.stages) != sorted(self.stages):
            raise ValueError("stages must be increasing percentages in (0, 50]")
        if not 0 < self.ab_treatment_percent <= 50:
            raise ValueError("ab_treatment_percent must be in (0, 50]")
        if self.look_every < 1 or self.min_stage_samples < 1:
            raise ValueError("look_every and min_stage_samples must be >= 1")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["stages"] = list(self.stages)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RolloutSpec":
        data = dict(payload)
        data["stages"] = tuple(data.get("stages") or ())
        data["gate"] = GateConfig(**(data.get("gate") or {}))
        return cls(**data)


class RolloutController:
    def __init__(
        self,
        registry: PolicyRegistry,
        db_path: Path,
        *,
        experiments: Any = None,
        audit: Optional[LearningAuditLog] = None,
        planner_applies_policy: bool = False,
        assigner: Optional[CohortAssigner] = None,
    ):
        self.registry = registry
        self.db_path = Path(db_path).expanduser()
        self.experiments = experiments
        self.audit = audit
        self.planner_applies_policy = planner_applies_policy
        self.assigner = assigner or CohortAssigner()
        self._init()

    # ---------------------------------------------------------------- storage
    def _connect(self) -> sqlite3.Connection:
        con = connect_sqlite(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def _init(self) -> None:
        with self._connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS learning_rollouts (
                  rollout_id TEXT PRIMARY KEY,
                  artifact_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  state_json TEXT NOT NULL
                )""")
            con.execute("CREATE INDEX IF NOT EXISTS learning_rollouts_status ON learning_rollouts(status)")
            con.execute("""
                CREATE TABLE IF NOT EXISTS learning_rollout_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  rollout_id TEXT NOT NULL,
                  at REAL NOT NULL,
                  event TEXT NOT NULL,
                  payload_json TEXT NOT NULL
                )""")

    # ------------------------------------------------------------------ start
    def start(self, artifact_id: str, *, actor: str, spec: Optional[RolloutSpec] = None) -> dict[str, Any]:
        spec = spec or RolloutSpec()
        if is_system_identity(actor):
            raise PermissionError("starting a rollout needs a human actor")
        if not self.planner_applies_policy:
            raise PermissionError("the planner does not apply learned policies yet; an online experiment would compare identical arms")
        artifact = self.registry.get(artifact_id)
        if artifact is None:
            raise KeyError(artifact_id)
        if artifact.status != "approved" or not artifact.approval_id:
            raise PermissionError(f"artefact {artifact_id} needs a stored human approval before real traffic sees it (status {artifact.status})")
        self.registry.approvals.require_approved(
            ApprovalRecord(proposal_id=artifact_id, approval_type=APPROVAL_TYPES[artifact.kind], approval_id=artifact.approval_id),
            content_sha256=artifact.content_sha256,
        )
        if (artifact.evaluation or {}).get("verdict") not in PASSING_VERDICTS:
            raise PermissionError("artefact needs a passing offline evaluation before an online rollout")
        if self.current() is not None:
            raise PermissionError("another rollout is running; stop it first")
        candidate = self.registry.candidate_policy(artifact_id)
        rollout_id = "ro_" + uuid.uuid4().hex[:16]
        percent = spec.stages[0] if spec.mode == "canary" else spec.ab_treatment_percent
        state = {
            "rollout_id": rollout_id,
            "artifact_id": artifact_id,
            "experiment_id": f"learning_rollout_{rollout_id}",
            "status": "running",
            "spec": spec.to_dict(),
            "stage_index": 0,
            "treatment_percent": percent,
            "base_version": self.registry.active_version(),
            "candidate_version": candidate.version,
            "looks": 0,
            "since_look": 0,
            "arms": {"control": asdict(ArmStats()), "treatment": asdict(ArmStats())},
            "waiting_approval": {"control": 0, "treatment": 0},
            "stage": {"n": 0, "errors": 0, "latencies": []},
            "started_by": actor,
            "decisions": [],
        }
        if self.experiments is not None:
            from omnidesk_agent.self_learning.experiments.experiment_manager import ExperimentSpec

            self.experiments.create(ExperimentSpec(
                experiment_id=state["experiment_id"],
                name=f"{spec.mode} rollout of {artifact.kind} {artifact.name}",
                control_policy=state["base_version"],
                treatment_policy=candidate.version,
                treatment_percent=percent,
                status="running",
                created_at=time.time(),
            ))
        self._save(state, event="started", payload={"actor": actor, "spec": spec.to_dict()})
        return self._public(state)

    # ------------------------------------------------------------- assignment
    def assign(self, msg: Any) -> PolicyAssignment:
        state = self.current()
        if state is None:
            return PolicyAssignment(policy=self.registry.active_policy())
        if self.registry.active_version() != state["base_version"]:
            self._finish(state, "stopped_base_changed", reason="the active policy changed during the rollout", actor=GUARD_ACTOR)
            return PolicyAssignment(policy=self.registry.active_policy())
        unit = unit_hash(msg)
        cohort = self.assigner.assign(state["experiment_id"], unit, treatment_percent=float(state["treatment_percent"]))
        if cohort.arm == "treatment":
            policy = self.registry.candidate_policy(state["artifact_id"])
            if policy.version != state["candidate_version"]:
                self._finish(state, "stopped_candidate_changed", reason="candidate policy changed", actor=GUARD_ACTOR)
                return PolicyAssignment(policy=self.registry.active_policy())
        else:
            policy = self.registry.active_policy()
        return PolicyAssignment(
            policy=policy,
            arm=cohort.arm,
            rollout_id=state["rollout_id"],
            experiment_id=state["experiment_id"],
            unit_hash=unit,
            bucket=cohort.bucket,
        )

    # ------------------------------------------------------------ observation
    def observe(self, assignment: PolicyAssignment, outcome: RunOutcome) -> Optional[dict[str, Any]]:
        if assignment.arm not in ("control", "treatment") or not assignment.rollout_id:
            return None
        state = self._load(assignment.rollout_id)
        if state is None or state["status"] != "running":
            return None
        spec = RolloutSpec.from_dict(state["spec"])
        arm = assignment.arm
        if not outcome.final:
            state["waiting_approval"][arm] += 1
            self._save(state)
            return None
        stats = ArmStats(**state["arms"][arm])
        stats.n += 1
        stats.successes += 1 if outcome.success else 0
        stats.safety_runs += 1 if outcome.policy_safety_event else 0
        if outcome.model_cost_usd is not None and math.isfinite(outcome.model_cost_usd) and outcome.model_cost_usd >= 0:
            stats.cost_sum += float(outcome.model_cost_usd)
            stats.cost_n += 1
        if outcome.latency_ms is not None and math.isfinite(outcome.latency_ms) and outcome.latency_ms >= 0:
            stats.latency_sum += float(outcome.latency_ms)
            stats.latency_n += 1
        state["arms"][arm] = asdict(stats)
        self._record_raw(state, assignment, outcome)

        if arm == "treatment":
            stage = state["stage"]
            stage["n"] += 1
            stage["errors"] += 0 if outcome.success else 1
            if outcome.latency_ms is not None and len(stage["latencies"]) < MAX_STAGE_LATENCIES:
                stage["latencies"].append(float(outcome.latency_ms))
            breach = self._slo_breach(spec, stage, ArmStats(**state["arms"]["control"])) if spec.mode == "canary" else None
            if breach:
                self._save(state)
                return self._rollback(state, reason=breach, actor=GUARD_ACTOR)
            state["since_look"] += 1
            if state["since_look"] >= spec.look_every:
                state["since_look"] = 0
                return self._look(state, spec)
        self._save(state)
        return None

    def _look(self, state: dict[str, Any], spec: RolloutSpec) -> dict[str, Any]:
        state["looks"] += 1
        control, treatment = ArmStats(**state["arms"]["control"]), ArmStats(**state["arms"]["treatment"])
        decision = SequentialGate(spec.gate).evaluate(control, treatment, look=state["looks"], mode=spec.mode)
        record = decision.to_dict()
        state["decisions"].append({"look": state["looks"], "decision": decision.decision, "reason": decision.reason, "at": time.time()})
        if decision.decision == "rollback":
            self._save(state, event="gate", payload=record)
            return self._rollback(state, reason=decision.reason, actor=GUARD_ACTOR)
        if decision.decision == "stop_inconclusive":
            self._save(state, event="gate", payload=record)
            return self._finish(state, "stopped_inconclusive", reason=decision.reason, actor=GUARD_ACTOR)
        if decision.decision == "promote_candidate":
            last = state["stage_index"] >= len(spec.stages) - 1
            if spec.mode == "canary" and not last and state["stage"]["n"] >= spec.min_stage_samples:
                state["stage_index"] += 1
                state["treatment_percent"] = spec.stages[state["stage_index"]]
                state["stage"] = {"n": 0, "errors": 0, "latencies": []}
                self._save(state, event="stage_advanced", payload={**record, "treatment_percent": state["treatment_percent"]})
                return self._public(state)
            if spec.mode == "ab" or last:
                self._save(state, event="gate", payload=record)
                return self._ready(state, spec, record)
        self._save(state, event="gate", payload=record)
        return self._public(state)

    # ---------------------------------------------------------------- outcomes
    def rollback(self, *, actor: str, reason: str) -> dict[str, Any]:
        """Manual stop of the running rollout; traffic returns to the active policy."""
        if is_system_identity(actor) or not str(reason or "").strip():
            raise PermissionError("a manual rollback needs a human actor and a reason")
        state = self.current()
        if state is None:
            raise PermissionError("no rollout is running")
        return self._rollback(state, reason=reason, actor=actor)

    def _rollback(self, state: dict[str, Any], *, reason: str, actor: str) -> dict[str, Any]:
        result = self._finish(state, "rolled_back", reason=reason, actor=actor)
        self.registry.mark_rolled_back(state["artifact_id"], actor=actor, reason=f"rollout {state['rollout_id']}: {reason}")
        if self.audit is not None:
            self.audit.append(LearningEvent(
                event_type="rollback_event",
                rollback_success=True,
                metadata={"rollout_id": state["rollout_id"], "artifact_id": state["artifact_id"], "reason": reason, "actor": actor},
            ))
        return result

    def _ready(self, state: dict[str, Any], spec: RolloutSpec, gate_record: dict[str, Any]) -> dict[str, Any]:
        treatment = state["arms"]["treatment"]
        self.registry.record_evaluation(state["artifact_id"], {
            "verdict": "improved" if spec.mode == "ab" else "non_inferior",
            "method": f"online_{spec.mode}_v1",
            "trace_count": int(treatment["n"]),
            "rollout_id": state["rollout_id"],
            "gate": gate_record,
        })
        return self._finish(state, "ready_to_promote", reason="gate passed; a human applies the artefact", actor=GUARD_ACTOR)

    def _finish(self, state: dict[str, Any], status: str, *, reason: str, actor: str) -> dict[str, Any]:
        state["status"] = status
        state["treatment_percent"] = 0.0
        state["finished_reason"] = reason
        state["finished_by"] = actor
        self._save(state, event=status, payload={"reason": reason, "actor": actor})
        if self.experiments is not None:
            try:
                spec = self.experiments.get(state["experiment_id"])
                if spec:
                    from omnidesk_agent.self_learning.experiments.experiment_manager import ExperimentSpec

                    fields = {k: spec[k] for k in ("experiment_id", "name", "control_policy", "treatment_policy", "created_at")}
                    self.experiments.create(ExperimentSpec(**fields, treatment_percent=0.0, status=status))
            except Exception:
                pass  # the rollout table is authoritative
        return self._public(state)

    @staticmethod
    def _slo_breach(spec: RolloutSpec, stage: dict[str, Any], control: ArmStats) -> Optional[str]:
        n = int(stage["n"])
        control_error = 1.0 - control.success_rate if control.n else 0.0
        error = stage["errors"] / n if n else 0.0
        if n >= min(20, spec.min_stage_samples) and error > spec.slo_max_error_rate and error > control_error:
            return f"canary error rate {error:.2%} exceeds SLO {spec.slo_max_error_rate:.0%} and control ({control_error:.2%})"
        latencies = sorted(stage["latencies"])
        if spec.slo_p95_latency_ms is not None and len(latencies) >= 20:
            p95 = latencies[min(len(latencies) - 1, int(math.ceil(0.95 * len(latencies))) - 1)]
            if p95 > spec.slo_p95_latency_ms:
                return f"canary p95 latency {p95:.0f} ms exceeds SLO {spec.slo_p95_latency_ms:.0f} ms"
        return None

    def _record_raw(self, state: dict[str, Any], assignment: PolicyAssignment, outcome: RunOutcome) -> None:
        if self.experiments is None:
            return
        try:
            self.experiments.record(ExperimentObservation(
                experiment_id=state["experiment_id"],
                unit_id=str(assignment.unit_hash),
                arm=assignment.arm,
                success=outcome.success,
                reward=1.0 if outcome.success else 0.0,
                cost=float(outcome.model_cost_usd or 0.0),
                latency_ms=float(outcome.latency_ms or 0.0),
                safety_violation=outcome.policy_safety_event,
                metadata={"status": outcome.status, "cost_measured": outcome.model_cost_usd is not None, "policy_version": assignment.policy.version},
            ))
        except Exception:
            pass  # raw rows are for dashboards; the aggregates decide

    # ----------------------------------------------------------------- reads
    def current(self) -> Optional[dict[str, Any]]:
        with self._connect() as con:
            row = con.execute("SELECT state_json FROM learning_rollouts WHERE status='running' ORDER BY created_at DESC LIMIT 1").fetchone()
        return None if row is None else json.loads(row["state_json"])

    def get(self, rollout_id: str) -> Optional[dict[str, Any]]:
        state = self._load(rollout_id)
        return None if state is None else self._public(state)

    def events(self, rollout_id: str) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute("SELECT at, event, payload_json FROM learning_rollout_events WHERE rollout_id=? ORDER BY id", (rollout_id,)).fetchall()
        return [{"at": r["at"], "event": r["event"], "payload": json.loads(r["payload_json"])} for r in rows]

    def _load(self, rollout_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as con:
            row = con.execute("SELECT state_json FROM learning_rollouts WHERE rollout_id=?", (rollout_id,)).fetchone()
        return None if row is None else json.loads(row["state_json"])

    def _save(self, state: dict[str, Any], *, event: Optional[str] = None, payload: Optional[dict[str, Any]] = None) -> None:
        now = time.time()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO learning_rollouts(rollout_id, artifact_id, status, created_at, updated_at, state_json)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(rollout_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at, state_json=excluded.state_json
                """,
                (state["rollout_id"], state["artifact_id"], state["status"], now, now, json.dumps(state)),
            )
            if event:
                con.execute(
                    "INSERT INTO learning_rollout_events(rollout_id, at, event, payload_json) VALUES(?,?,?,?)",
                    (state["rollout_id"], now, event, json.dumps(payload or {}, default=str)),
                )

    @staticmethod
    def _public(state: dict[str, Any]) -> dict[str, Any]:
        out = dict(state)
        stage = dict(out.get("stage") or {})
        stage["latency_samples"] = len(stage.pop("latencies", []) or [])
        out["stage"] = stage
        return out


def unit_hash(msg: Any) -> str:
    """Randomization unit: the conversation thread (or message), never message text."""
    channel = str(getattr(msg, "channel", "") or "")
    key = getattr(msg, "thread_id", None) or getattr(msg, "message_id", None) or uuid.uuid4().hex
    return hashlib.sha256(f"{channel}:{key}".encode("utf-8")).hexdigest()[:32]


__all__ = ["RolloutController", "RolloutSpec", "unit_hash", "GUARD_ACTOR"]
