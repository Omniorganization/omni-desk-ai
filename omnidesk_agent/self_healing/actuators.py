"""Actuators carry out one kind of recovery decision through public APIs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol

from omnidesk_agent.self_healing.policy import RecoveryDecision, RuntimeSignal
from omnidesk_agent.self_healing.probes import ConfigSnapshots, WorkerRegistry, is_transient


@dataclass
class ActionResult:
    ok: bool
    action: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Actuator(Protocol):
    actions: frozenset[str]

    async def execute(self, decision: RecoveryDecision, signal: RuntimeSignal) -> ActionResult: ...


async def _maybe_await(value: Any) -> Any:
    return await value if asyncio.iscoroutine(value) else value


class WorkerRestartActuator:
    actions = frozenset({"restart_worker"})

    def __init__(self, registry: WorkerRegistry, *, settle_seconds: float = 0.05):
        self.registry = registry
        self.settle_seconds = settle_seconds

    async def execute(self, decision: RecoveryDecision, signal: RuntimeSignal) -> ActionResult:
        name = decision.component.split(":", 1)[1] if ":" in decision.component else decision.component
        handle = self.registry.get(name)
        if handle is None:
            return ActionResult(False, "restart_worker", {"error": f"no registered worker {name}"})
        try:
            await _maybe_await(handle.stop())
        except Exception as exc:  # a crashed worker may fail to stop cleanly
            stop_error = type(exc).__name__
        else:
            stop_error = None
        await _maybe_await(handle.start())
        await asyncio.sleep(self.settle_seconds)
        alive = bool(handle.is_alive())
        return ActionResult(alive, "restart_worker", {"worker": name, "alive_after": alive, "stop_error": stop_error})


class StaleJobActuator:
    """Return jobs whose lease expired to retry (or dead-letter after max retries).

    A worker that holds a stale job is stuck, so it is restarted first; the
    cancelled run cannot then finish the job behind the retry's back.
    """

    actions = frozenset({"recover_stale_jobs"})

    def __init__(self, queue: Any, *, lease_seconds: int = 300, registry: Optional[WorkerRegistry] = None, worker_name: Optional[str] = None):
        self.queue = queue
        self.lease_seconds = int(lease_seconds)
        self.registry = registry
        self.worker_name = worker_name

    async def execute(self, decision: RecoveryDecision, signal: RuntimeSignal) -> ActionResult:
        restarted = False
        handle = self.registry.get(self.worker_name) if (self.registry and self.worker_name) else None
        if handle is not None and handle.is_alive():
            await _maybe_await(handle.stop())
            restarted = True
        try:
            recovered = await asyncio.to_thread(self.queue.recover_stale_running, lease_seconds=self.lease_seconds)
        finally:
            if handle is not None and restarted:
                # Never leave a worker stopped because recovery failed or timed out.
                await _maybe_await(handle.start())
        return ActionResult(True, "recover_stale_jobs", {"recovered": int(recovered), "worker_restarted": restarted})


class DeadLetterRequeueActuator:
    """Requeue dead-lettered jobs whose last error was transient, at most N per action."""

    actions = frozenset({"requeue_dead_letters"})

    def __init__(self, queue: Any, *, max_per_action: int = 20):
        self.queue = queue
        self.max_per_action = max(1, int(max_per_action))

    async def execute(self, decision: RecoveryDecision, signal: RuntimeSignal) -> ActionResult:
        dead = await asyncio.to_thread(self.queue.list_dead_letters, limit=200)
        requeued, skipped = [], 0
        for job in dead:
            if len(requeued) >= self.max_per_action:
                break
            if not is_transient(job.get("last_error")):
                skipped += 1
                continue
            await asyncio.to_thread(self.queue.requeue_dead_letter, str(job["id"]))
            requeued.append(str(job["id"]))
        # A bounded batch is progress even when more transient jobs remain.
        return ActionResult(bool(requeued), "requeue_dead_letters", {"requeued": requeued, "left_for_human": skipped, "progress": bool(requeued)})


class CircuitActuator:
    """Force a model profile's circuit open so the router uses its fallback."""

    actions = frozenset({"switch_model_profile", "open_circuit_breaker"})

    def __init__(self, router: Any):
        self.router = router

    async def execute(self, decision: RecoveryDecision, signal: RuntimeSignal) -> ActionResult:
        opener = getattr(self.router, "open_circuit", None)
        profile = decision.component.split(":", 1)[1] if ":" in decision.component else decision.component
        if not callable(opener):
            return ActionResult(False, decision.action, {"error": "router has no public open_circuit API", "profile": profile})
        opener(profile, reason=decision.reason)
        return ActionResult(True, decision.action, {"profile": profile})


class ConfigRollbackActuator:
    actions = frozenset({"rollback_config"})

    def __init__(self, snapshots: ConfigSnapshots, config_path: Path, *, restart: Optional[Callable[[], Any]] = None):
        self.snapshots = snapshots
        self.config_path = Path(config_path).expanduser()
        self.restart = restart

    async def execute(self, decision: RecoveryDecision, signal: RuntimeSignal) -> ActionResult:
        if not decision.rollback_ref:
            return ActionResult(False, "rollback_config", {"error": "no known-good snapshot"})
        kept = await asyncio.to_thread(self.snapshots.restore, decision.rollback_ref, self.config_path)
        restarted = None
        if self.restart is not None:
            restarted = await _maybe_await(self.restart())
        return ActionResult(True, "rollback_config", {"snapshot": decision.rollback_ref, "rejected_copy": str(kept), "restart": restarted})


class RepairRequestActuator:
    """Turn a repeated failure into one repair request for the self-upgrade pipeline.

    Requests are deduplicated per component and day. ``file_repair`` (the
    self-upgrade pipeline) opens an ``ai/*`` PR for human review; without it the
    request waits in ``<root>/repair_requests`` for the pipeline to pick up.
    """

    actions = frozenset({"create_upgrade_proposal"})

    def __init__(self, root: Path, *, file_repair: Optional[Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = None, clock: Callable[[], float] = time.time):
        self.root = Path(root).expanduser() / "repair_requests"
        self.root.mkdir(parents=True, exist_ok=True)
        self.file_repair = file_repair
        self.clock = clock

    async def execute(self, decision: RecoveryDecision, signal: RuntimeSignal) -> ActionResult:
        day = time.strftime("%Y%m%d", time.gmtime(self.clock()))
        fingerprint = hashlib.sha256(f"{decision.component}:{day}".encode()).hexdigest()[:16]
        path = self.root / f"{fingerprint}.json"
        if path.exists():
            return ActionResult(True, "create_upgrade_proposal", {"deduplicated": True, "request": str(path)})
        request = {
            "fingerprint": fingerprint,
            "component": decision.component,
            "reason": decision.reason,
            "signal": {"error_rate": signal.error_rate, "consecutive_failures": signal.consecutive_failures, "metadata": signal.metadata},
            "created_at": self.clock(),
            "status": "pending",
        }
        if self.file_repair is not None:
            try:
                request["pipeline"] = await self.file_repair(request)
                request["status"] = str(request["pipeline"].get("status", "submitted"))
            except Exception as exc:
                request["status"] = "pipeline_error"
                request["pipeline"] = {"error_class": type(exc).__name__, "error": str(exc)[:500]}
        path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        return ActionResult(request["status"] != "pipeline_error", "create_upgrade_proposal", {"request": str(path), "status": request["status"]})


class EscalationActuator:
    """Record decisions a human must take and notify, if a notifier is configured."""

    actions = frozenset({"escalate_human_approval", "rollback_release", "disable_capability"})

    def __init__(self, root: Path, *, notify: Optional[Callable[[dict[str, Any]], Any]] = None):
        self.path = Path(root).expanduser() / "escalations.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.notify = notify

    async def execute(self, decision: RecoveryDecision, signal: RuntimeSignal) -> ActionResult:
        record = {"at": time.time(), "decision": decision.to_dict(), "health_ok": signal.health_ok}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        notified = False
        if self.notify is not None:
            try:
                await _maybe_await(self.notify(record))
                notified = True
            except Exception:
                notified = False
        return ActionResult(True, decision.action, {"escalated": True, "notified": notified})
