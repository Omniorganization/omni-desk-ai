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
from omnidesk_agent.self_healing.probes import RequeueLedger, WorkerRegistry, is_transient, single_consumer


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

    def __init__(self, registry: WorkerRegistry, *, settle_seconds: float = 0.05, clock: Callable[[], float] = time.time):
        self.registry = registry
        self.settle_seconds = settle_seconds
        self.clock = clock

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
        try:
            still_running = bool(handle.is_alive())
        except Exception:
            still_running = True  # cannot tell: treat it as running
        if still_running:
            # Its jobs must not be marked orphaned while it may still run them.
            return ActionResult(False, "restart_worker", {"worker": name, "error": "worker still running after stop()", "stop_error": stop_error})
        # Jobs the stopped worker claimed before this moment have no live worker any more.
        self.registry.mark_restarted(name, self.clock())
        await _maybe_await(handle.start())
        await asyncio.sleep(self.settle_seconds)
        alive = bool(handle.is_alive())
        return ActionResult(alive, "restart_worker", {"worker": name, "alive_after": alive, "stop_error": stop_error})


class StaleJobActuator:
    """Return orphaned jobs past their lease to retry (or dead-letter after max retries).

    Only jobs claimed before ``orphaned_before()`` (the last restart of the
    queue's only worker) are touched, one by one after re-reading each, so a
    healthy run that outlives its lease is never re-run behind its back. No
    worker is stopped. Without that time (a shared queue) the policy escalates
    instead and this actuator refuses.
    """

    actions = frozenset({"recover_stale_jobs"})

    def __init__(self, queue: Any, *, orphaned_before: Callable[[], Optional[float]], lease_seconds: int = 300, clock: Callable[[], float] = time.time):
        if not single_consumer(queue):
            raise ValueError("stale-job recovery is only safe for a queue this process alone consumes (the SQLite JobQueue)")
        self.queue = queue
        self.orphaned_before = orphaned_before
        self.lease_seconds = int(lease_seconds)
        self.clock = clock

    async def execute(self, decision: RecoveryDecision, signal: RuntimeSignal) -> ActionResult:
        boundary = self.orphaned_before()
        if boundary is None:
            return ActionResult(False, "recover_stale_jobs", {"error": "no worker restart is recorded, so no running job is known to be orphaned"})
        cutoff = min(self.clock() - self.lease_seconds, boundary)
        running = await asyncio.to_thread(self.queue.list, status="running", limit=200)
        recovered = []
        for job in running:
            locked_at = job.get("locked_at")
            if locked_at is None or float(locked_at) > cutoff:
                continue
            current = await asyncio.to_thread(self.queue.get, str(job["id"]))
            if not current or current.get("status") != "running" or current.get("locked_at") != locked_at:
                continue  # it moved on since the listing
            await asyncio.to_thread(self.queue.fail, str(job["id"]), f"orphaned: claimed before its worker was restarted and held past its {self.lease_seconds}s lease")
            recovered.append(str(job["id"]))
        return ActionResult(True, "recover_stale_jobs", {"recovered": recovered, "claimed_before": boundary})


class DeadLetterRequeueActuator:
    """Requeue dead-lettered jobs whose last error was transient, at most N per action
    and at most once per job across every replica (``RequeueLedger.claim``); the rest
    stay for a human."""

    actions = frozenset({"requeue_dead_letters"})

    def __init__(self, queue: Any, *, ledger: RequeueLedger, max_per_action: int = 20):
        self.queue = queue
        self.ledger = ledger
        self.max_per_action = max(1, int(max_per_action))

    async def execute(self, decision: RecoveryDecision, signal: RuntimeSignal) -> ActionResult:
        dead = await asyncio.to_thread(self.queue.list_dead_letters, limit=200)
        requeued, skipped, capped = [], 0, 0
        for job in dead:
            if len(requeued) >= self.max_per_action:
                break
            if not is_transient(job.get("last_error")):
                skipped += 1
                continue
            # Claim first, atomically: a job another replica (or an earlier run)
            # requeued is not claimed again, and if the requeue then fails the
            # job has used its one automatic requeue (fail closed).
            claimed = await asyncio.to_thread(self.ledger.claim, str(job["id"]), last_error=job.get("last_error"))
            if not claimed:
                capped += 1
                continue
            await asyncio.to_thread(self.queue.requeue_dead_letter, str(job["id"]))
            requeued.append(str(job["id"]))
        # A bounded batch is progress even when more transient jobs remain.
        return ActionResult(bool(requeued), "requeue_dead_letters", {
            "requeued": requeued, "left_for_human": skipped, "already_requeued": capped, "progress": bool(requeued),
        })


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


class RepairRequestActuator:
    """Turn a repeated failure into one repair request for the self-upgrade pipeline.

    Requests are deduplicated per component and day. ``file_repair`` (the
    self-upgrade pipeline) opens an ``ai/*`` PR for human review; without it the
    request waits in ``<root>/repair_requests`` for the pipeline to pick up.
    """

    actions = frozenset({"create_upgrade_proposal"})
    requests_human = True  # files a request for review; changes nothing itself

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
    requests_human = True  # records and notifies; changes nothing itself

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
