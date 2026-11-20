"""Build the in-process self-healing supervisor the daemon runs.

Mode comes from ``OMNIDESK_SELF_HEALING_MODE`` (off | observe | act), default
``observe``: decisions are audited, nothing is changed until an operator
switches to ``act``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Optional

from omnidesk_agent.self_healing.actuators import (
    CircuitActuator,
    DeadLetterRequeueActuator,
    EscalationActuator,
    RepairRequestActuator,
    StaleJobActuator,
    WorkerRestartActuator,
)
from omnidesk_agent.self_healing.probes import JobQueueProbe, ModelRouterProbe, WorkerProbe, WorkerRegistry
from omnidesk_agent.self_healing.supervisor import HealingSupervisor

MODE_ENV = "OMNIDESK_SELF_HEALING_MODE"


def worker_is_alive(worker: Any) -> bool:
    task = getattr(worker, "_task", None)
    return task is not None and not task.done()


def build_healing_supervisor(
    workspace_root: Path,
    *,
    job_queue: Any,
    router: Any = None,
    workers: Iterable[tuple[str, Any]] = (),
    lease_seconds: int = 300,
    mode: Optional[str] = None,
) -> HealingSupervisor:
    root = Path(workspace_root).expanduser() / "self_healing"
    registry = WorkerRegistry()
    worker_names = []
    for name, worker in workers:
        registry.register(name, start=worker.start, stop=worker.stop, is_alive=lambda w=worker: worker_is_alive(w))
        worker_names.append(name)
    probes: list[Any] = [WorkerProbe(registry), JobQueueProbe(job_queue, lease_seconds=lease_seconds)]
    actuators: list[Any] = [
        WorkerRestartActuator(registry),
        StaleJobActuator(job_queue, lease_seconds=lease_seconds, registry=registry, worker_name=worker_names[0] if worker_names else None),
        DeadLetterRequeueActuator(job_queue),
        RepairRequestActuator(root),
        EscalationActuator(root),
    ]
    if router is not None:
        probes.append(ModelRouterProbe(router))
        actuators.append(CircuitActuator(router))
    chosen = (mode or os.getenv(MODE_ENV) or "observe").strip().lower()
    if chosen not in {"off", "observe", "act"}:
        chosen = "observe"
    return HealingSupervisor(probes, actuators, audit_path=root / "healing.jsonl", mode=chosen)  # type: ignore[arg-type]
