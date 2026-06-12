from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from omnidesk_agent.self_learning.experiments.cohort_assignment import CohortAssigner, CohortAssignment
from omnidesk_agent.self_learning.experiments.metric_collector import ExperimentMetricCollector, ExperimentObservation
from omnidesk_agent.self_learning.experiments.winner_selector import WinnerDecision, WinnerSelector
from omnidesk_agent.storage.migrations import Migration, apply_migrations
from omnidesk_agent.storage.sqlite import connect_sqlite


@dataclass(frozen=True)
class ExperimentSpec:
    experiment_id: str
    name: str
    control_policy: str
    treatment_policy: str
    treatment_percent: float = 10.0
    status: str = "running"
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["created_at"] = self.created_at or time.time()
        return payload


class ExperimentManager:
    """SQLite-backed A/B framework for learning policy changes."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.assigner = CohortAssigner()
        self.collector = ExperimentMetricCollector()
        self.selector = WinnerSelector()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = connect_sqlite(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS learning_experiments (
                  experiment_id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  control_policy TEXT NOT NULL,
                  treatment_policy TEXT NOT NULL,
                  treatment_percent REAL NOT NULL,
                  status TEXT NOT NULL,
                  created_at REAL NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS learning_experiment_observations (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  experiment_id TEXT NOT NULL,
                  unit_id TEXT NOT NULL,
                  arm TEXT NOT NULL,
                  success INTEGER NOT NULL,
                  reward REAL NOT NULL,
                  cost REAL NOT NULL,
                  latency_ms REAL NOT NULL,
                  safety_violation INTEGER NOT NULL,
                  metadata TEXT,
                  created_at REAL NOT NULL
                )
                """
            )
            apply_migrations(con, [Migration(1, "learning_experiment_schema_baseline", lambda _con: None)])

    def create(self, spec: ExperimentSpec) -> dict[str, Any]:
        if spec.treatment_percent < 0 or spec.treatment_percent > 100:
            raise ValueError("treatment_percent must be between 0 and 100")
        payload = spec.to_dict()
        with self._connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO learning_experiments(
                  experiment_id, name, control_policy, treatment_policy, treatment_percent, status, created_at
                ) VALUES(:experiment_id, :name, :control_policy, :treatment_policy, :treatment_percent, :status, :created_at)
                """,
                payload,
            )
        return payload

    def get(self, experiment_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as con:
            row = con.execute("SELECT * FROM learning_experiments WHERE experiment_id=?", (experiment_id,)).fetchone()
        return dict(row) if row else None

    def assign(self, experiment_id: str, unit_id: str) -> CohortAssignment:
        spec = self.get(experiment_id)
        if not spec or spec.get("status") != "running":
            raise ValueError("experiment is not running")
        return self.assigner.assign(experiment_id, unit_id, treatment_percent=float(spec["treatment_percent"]))

    def record(self, observation: ExperimentObservation) -> dict[str, Any]:
        payload = observation.to_dict()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO learning_experiment_observations(
                  experiment_id, unit_id, arm, success, reward, cost, latency_ms, safety_violation, metadata, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    observation.experiment_id,
                    observation.unit_id,
                    observation.arm,
                    1 if observation.success else 0,
                    float(observation.reward),
                    float(observation.cost),
                    float(observation.latency_ms),
                    1 if observation.safety_violation else 0,
                    json.dumps(observation.metadata or {}, ensure_ascii=False),
                    time.time(),
                ),
            )
        return payload

    def observations(self, experiment_id: str) -> list[ExperimentObservation]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM learning_experiment_observations WHERE experiment_id=?", (experiment_id,)).fetchall()
        output = []
        for row in rows:
            output.append(
                ExperimentObservation(
                    experiment_id=row["experiment_id"],
                    unit_id=row["unit_id"],
                    arm=row["arm"],
                    success=bool(row["success"]),
                    reward=float(row["reward"]),
                    cost=float(row["cost"]),
                    latency_ms=float(row["latency_ms"]),
                    safety_violation=bool(row["safety_violation"]),
                    metadata=json.loads(row["metadata"] or "{}"),
                )
            )
        return output

    def summary(self, experiment_id: str) -> dict[str, dict[str, float]]:
        return self.collector.summarize(self.observations(experiment_id))

    def select_winner(self, experiment_id: str, **kwargs: Any) -> WinnerDecision:
        return self.selector.select(self.summary(experiment_id), **kwargs)
