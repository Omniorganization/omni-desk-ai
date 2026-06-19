from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

Arm = Literal["control", "treatment"]


@dataclass(frozen=True)
class CohortAssignment:
    experiment_id: str
    unit_id: str
    arm: Arm
    bucket: int
    treatment_percent: float


class CohortAssigner:
    """Deterministic traffic splitter for learning experiments.

    The same experiment/unit pair always receives the same cohort, which keeps
    replay and production metrics comparable and avoids user/task oscillation.
    """

    def assign(self, experiment_id: str, unit_id: str, *, treatment_percent: float) -> CohortAssignment:
        if not experiment_id.strip():
            raise ValueError("experiment_id is required")
        if not unit_id.strip():
            raise ValueError("unit_id is required")
        if treatment_percent < 0 or treatment_percent > 100:
            raise ValueError("treatment_percent must be between 0 and 100")
        digest = hashlib.sha256(f"{experiment_id}:{unit_id}".encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) % 100
        arm: Arm = "treatment" if bucket < treatment_percent else "control"
        return CohortAssignment(experiment_id, unit_id, arm, bucket, treatment_percent)
