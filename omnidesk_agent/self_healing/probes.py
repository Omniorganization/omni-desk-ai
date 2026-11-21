"""Health probes: turn runtime state into ``RuntimeSignal``s.

Each probe reads state through public APIs only (worker handles, JobQueue,
ModelRouter.status, an HTTP health URL, a config validator) and never acts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional, Union

from omnidesk_agent.core.job_queue import JobQueue
from omnidesk_agent.self_healing.policy import RuntimeSignal
from omnidesk_agent.storage.sqlite import connect_sqlite

# Failures worth retrying once the dependency is back: timeouts, connection
# errors, rate limits and gateway errors. Status codes count only next to
# "HTTP"/"status" or with their reason phrase, never as bare digits, which also
# occur in order numbers and IDs. The queue's own crash-recovery message is not
# transient: a job that hung or crashed its worker is left for a human.
_TRANSIENT = re.compile(
    r"\btimed out\b"
    r"|\btemporar(?:y|ily) (?:unavailable|failure)\b"
    r"|\bconnection (?:reset|refused|aborted)\b"
    r"|\b(?:connecterror|connectionerror|remotedisconnected)\b"
    r"|\bremote end closed connection\b"
    r"|\brate[ -]?limit(?:ed)?\b|\btoo many requests\b"
    r"|\b(?:502:? bad gateway|503:? service unavailable|504:? gateway time-?out)\b"
    r"|\b(?:http(?:[ _]?error)?|status(?:[ _]?code)?)[\s:=]*(?:429|502|503|504)\b"
)


def is_transient(error: Optional[str]) -> bool:
    return bool(_TRANSIENT.search(str(error or "").lower()))


@dataclass
class WorkerHandle:
    name: str
    start: Callable[[], Any]
    stop: Callable[[], Any]
    is_alive: Callable[[], bool]
    # False when start() would recover a queue other gateways share (the webhook
    # worker on PostgreSQL): its queue-wide lease recovery could re-run another
    # replica's healthy job, so a dead worker is escalated instead of restarted.
    restartable: bool = True


class WorkerRegistry:
    """Long-running workers the daemon registers so they can be restarted."""

    def __init__(self) -> None:
        self._handles: dict[str, WorkerHandle] = {}
        self._restarted_at: dict[str, float] = {}

    def register(self, name: str, *, start: Callable[[], Any], stop: Callable[[], Any], is_alive: Callable[[], bool], restartable: bool = True) -> None:
        self._handles[name] = WorkerHandle(name, start, stop, is_alive, restartable)

    def get(self, name: str) -> Optional[WorkerHandle]:
        return self._handles.get(name)

    def handles(self) -> list[WorkerHandle]:
        return list(self._handles.values())

    def mark_restarted(self, name: str, at: float) -> None:
        self._restarted_at[name] = at

    def restarted_at(self, name: str) -> Optional[float]:
        """When the healer last restarted ``name``: jobs it claimed before then have no live worker."""
        return self._restarted_at.get(name)


class RequeueLedger:
    """Automatic dead-letter requeues, kept in the queue's own store.

    ``JobQueue.requeue_dead_letter`` resets a job's retry count and last error,
    so without a record a job that keeps failing would be requeued forever. The
    record lives next to the jobs (a table in the SQLite queue's file, a
    namespace in the PostgreSQL state the queue uses) and is claimed atomically,
    so every replica and every pod generation gets the same answer: a job is
    requeued automatically at most once. Each claim keeps the error the queue
    row loses; claims older than ``retention_seconds`` are pruned.
    """

    NAMESPACE = "self_healing_requeues"

    def __init__(self, queue: Any, *, retention_seconds: float = 30 * 86400, clock: Callable[[], float] = time.time):
        state = getattr(queue, "state", None)
        if state is not None and callable(getattr(state, "insert_once", None)):
            self._backend: Any = _StateLedger(state, self.NAMESPACE)
        elif getattr(queue, "db_path", None) is not None:
            self._backend = _SqliteLedger(Path(queue.db_path))
        else:
            raise TypeError("the requeue ledger needs the queue's own store: the SQLite JobQueue or PostgresJobQueue")
        self.retention_seconds = float(retention_seconds)
        self.clock = clock

    def exhausted(self, job_ids: Iterable[str]) -> set[str]:
        """The given jobs that were requeued automatically before."""
        ids = sorted({str(job_id) for job_id in job_ids})
        try:
            return self._backend.claimed(ids)
        except Exception:
            return set(ids)  # an unreadable ledger allows no automatic requeue

    def claim(self, job_id: str, *, last_error: Optional[str]) -> bool:
        """True only for the first automatic requeue of ``job_id``, across every replica."""
        now = self.clock()
        self._backend.prune(now - self.retention_seconds)
        return bool(self._backend.insert_once(str(job_id), {"at": now, "last_error": str(last_error or "")[:500]}))


class _SqliteLedger:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        with connect_sqlite(db_path) as con:
            con.execute("CREATE TABLE IF NOT EXISTS self_healing_requeues (job_id TEXT PRIMARY KEY, at REAL NOT NULL, last_error TEXT)")

    def claimed(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        marks = ",".join("?" for _ in ids)
        query = f"SELECT job_id FROM self_healing_requeues WHERE job_id IN ({marks})"  # nosec B608
        # (only "?" placeholders are formatted in; the ids are bound parameters)
        with connect_sqlite(self.db_path) as con:
            return {str(row[0]) for row in con.execute(query, ids).fetchall()}

    def insert_once(self, job_id: str, record: dict[str, Any]) -> bool:
        with connect_sqlite(self.db_path) as con:
            cursor = con.execute("INSERT OR IGNORE INTO self_healing_requeues(job_id, at, last_error) VALUES (?, ?, ?)",
                                 (job_id, record["at"], record["last_error"]))
            return cursor.rowcount == 1

    def prune(self, before: float) -> None:
        with connect_sqlite(self.db_path) as con:
            con.execute("DELETE FROM self_healing_requeues WHERE at < ?", (before,))


class _StateLedger:
    """The PostgreSQL state store's atomic insert-if-absent, in a namespace of its own."""

    def __init__(self, state: Any, namespace: str):
        self.state = state
        self.namespace = namespace

    def claimed(self, ids: list[str]) -> set[str]:
        return {job_id for job_id in ids if self.state.get(self.namespace, job_id) is not None}

    def insert_once(self, job_id: str, record: dict[str, Any]) -> bool:
        return bool(self.state.insert_once(self.namespace, job_id, record))

    def prune(self, before: float) -> None:
        self.state.delete_stale(self.namespace, stale_before=before)


def single_consumer(queue: Any) -> bool:
    """Only the SQLite JobQueue is consumed by this process alone; a test double may say so itself."""
    return isinstance(queue, JobQueue) or getattr(queue, "single_consumer", False) is True


class WorkerProbe:
    name = "workers"

    def __init__(self, registry: WorkerRegistry):
        self.registry = registry

    async def collect(self) -> list[RuntimeSignal]:
        signals = []
        for handle in self.registry.handles():
            try:
                alive = bool(handle.is_alive())
            except Exception:
                alive = False
            signals.append(RuntimeSignal(component=f"worker:{handle.name}", health_ok=alive, metadata={"restartable": handle.restartable}))
        return signals


class JobQueueProbe:
    """Stale running jobs and dead letters.

    ``dependency_healthy`` must report whether the jobs' dependency (the model
    provider, a tool backend) works again; there is no default. Pass
    ``orphaned_before`` only for a queue this process alone consumes (the
    SQLite queue): it returns the time before which no running job has a live
    worker, e.g. ``lambda: registry.restarted_at("webhook")``; it is refused
    for any other queue. For a shared queue (PostgreSQL behind several
    gateways) a job past its lease may still be running elsewhere, so stale
    jobs are escalated, never recovered. Jobs the healer requeued once and that
    are dead-lettered again, whatever their new error, are reported as
    ``queue:dead_letters_exhausted`` so their escalation never waits behind the
    requeues of other jobs.
    """

    name = "job_queue"

    def __init__(
        self,
        queue: Any,
        *,
        dependency_healthy: Callable[[], bool],
        ledger: RequeueLedger,
        lease_seconds: int = 300,
        orphaned_before: Optional[Callable[[], Optional[float]]] = None,
        clock: Callable[[], float] = time.time,
    ):
        if orphaned_before is not None and not single_consumer(queue):
            raise ValueError("orphaned_before is only safe for a queue this process alone consumes (the SQLite JobQueue)")
        self.queue = queue
        self.dependency_healthy = dependency_healthy
        self.ledger = ledger
        self.lease_seconds = int(lease_seconds)
        self.orphaned_before = orphaned_before
        self.clock = clock

    async def collect(self) -> list[RuntimeSignal]:
        running, dead = await asyncio.gather(
            asyncio.to_thread(self.queue.list, status="running", limit=200),
            asyncio.to_thread(self.queue.list_dead_letters, limit=200),
        )
        cutoff = self.clock() - self.lease_seconds
        stale = [job for job in running if job.get("locked_at") is not None and float(job["locked_at"]) <= cutoff]
        boundary = self.orphaned_before() if self.orphaned_before else None
        orphaned = [job for job in stale if boundary is not None and float(job["locked_at"]) <= boundary]
        exhausted = self.ledger.exhausted(str(job["id"]) for job in dead)
        transient = [job for job in dead if is_transient(job.get("last_error"))]
        retryable = [job for job in transient if str(job["id"]) not in exhausted]
        try:
            healthy = bool(self.dependency_healthy())
        except Exception:
            healthy = False
        return [
            RuntimeSignal(component="queue:stale_jobs", health_ok=not stale, metadata={"stale_count": len(stale), "orphaned_count": len(orphaned)}),
            RuntimeSignal(
                component="queue:dead_letters_exhausted",
                health_ok=not exhausted,
                metadata={"exhausted_count": len(exhausted), "job_ids": sorted(exhausted)[:20]},
            ),
            RuntimeSignal(
                component="queue:dead_letters",
                health_ok=not retryable and len(dead) < 200,
                metadata={
                    "transient_count": len(transient),
                    "retryable_count": len(retryable),
                    "dead_letter_count": len(dead),
                    "dependency_healthy": healthy,
                    # JobQueue.list returns the newest 200 only.
                    "scan_truncated": len(dead) >= 200,
                },
            ),
        ]


class ModelRouterProbe:
    name = "model_router"

    def __init__(self, router: Any):
        self.router = router

    async def collect(self) -> list[RuntimeSignal]:
        status = self.router.status() if hasattr(self.router, "status") else {}
        signals = []
        for profile, state in sorted((status.get("circuit") or {}).items()):
            failures = int(float((state or {}).get("failures", 0)))
            signals.append(RuntimeSignal(component=f"model:{profile}", consecutive_failures=failures, health_ok=failures == 0))
        return signals


class HttpHealthProbe:
    """GET a health URL; any non-2xx, timeout or connection error is unhealthy."""

    def __init__(self, url: str, *, component: str = "gateway", timeout_seconds: float = 5.0, rollback_ref: Optional[Callable[[], Optional[str]]] = None):
        if not url.startswith(("http://", "https://")):
            raise ValueError("health URL must be http(s)")
        self.url = url
        self.component = component
        self.timeout = float(timeout_seconds)
        self.rollback_ref = rollback_ref
        self.name = f"http:{component}"

    def _get(self) -> int:
        request = urllib.request.Request(self.url, headers={"User-Agent": "omnidesk-self-healing"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # nosec B310
            return int(response.status)

    async def collect(self) -> list[RuntimeSignal]:
        try:
            code = await asyncio.to_thread(self._get)
            ok, detail = 200 <= code < 300, {"status_code": code}
        except Exception as exc:
            ok, detail = False, {"error_class": type(exc).__name__}
        ref = self.rollback_ref() if (self.rollback_ref and not ok) else None
        return [RuntimeSignal(component=self.component, health_ok=ok, consecutive_failures=0 if ok else 1, rollback_ref=ref, metadata=detail)]


class ConfigSnapshots:
    """Last-known-good copies of a config file, written atomically and readable by their owner only."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def save_known_good(self, data: bytes) -> str:
        """Save exactly these bytes (the ones that were checked) as known-good."""
        digest = hashlib.sha256(data).hexdigest()[:16]
        target = self.root / f"{digest}.snapshot"
        if not target.exists():
            _atomic_write(target, data)
        _atomic_write(self.root / "KNOWN_GOOD", json.dumps({"snapshot": digest, "saved_at": time.time()}).encode())
        return digest

    def known_good(self) -> Optional[str]:
        marker = self.root / "KNOWN_GOOD"
        if not marker.exists():
            return None
        snapshot = str(json.loads(marker.read_text()).get("snapshot") or "")
        return snapshot if (self.root / f"{snapshot}.snapshot").exists() else None

    def restore(self, snapshot: str, config_path: Path) -> Path:
        if not snapshot or not all(c in "0123456789abcdef" for c in snapshot):
            raise ValueError("invalid snapshot id")
        source = self.root / f"{snapshot}.snapshot"
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest()[:16] != snapshot:
            raise ValueError("snapshot content does not match its id")
        config_path = Path(config_path)
        rejected = None
        mode, owner = 0o600, None
        if config_path.exists():
            info = config_path.stat()
            mode, owner = stat.S_IMODE(info.st_mode), (info.st_uid, info.st_gid)
            rejected = self.root / f"rejected-{int(time.time())}-{hashlib.sha256(config_path.read_bytes()).hexdigest()[:8]}.config"
            shutil.copy2(config_path, rejected)
        _atomic_write(config_path, data, mode=mode, owner=owner)  # keep the replaced file's mode and owner
        return rejected or config_path


class ConfigFileProbe:
    name = "config"

    def __init__(self, config_path: Path, validate: Callable[[Path], Union[None, Awaitable[None]]], snapshots: ConfigSnapshots):
        self.config_path = Path(config_path).expanduser()
        self.validate = validate
        self.snapshots = snapshots

    async def collect(self) -> list[RuntimeSignal]:
        try:
            result = self.validate(self.config_path)
            if asyncio.iscoroutine(result):
                await result
            return [RuntimeSignal(component="config", health_ok=True)]
        except Exception as exc:
            return [RuntimeSignal(
                component="config",
                health_ok=False,
                rollback_ref=self.snapshots.known_good(),
                metadata={"error_class": type(exc).__name__},
            )]


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600, owner: Optional[tuple[int, int]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    if owner is not None and hasattr(os, "chown") and hasattr(os, "geteuid") and os.geteuid() == 0:
        os.chown(tmp, *owner)  # a root watchdog must not leave a root-owned config behind
    os.replace(tmp, path)
