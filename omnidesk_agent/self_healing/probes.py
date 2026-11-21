"""Health probes: turn runtime state into ``RuntimeSignal``s.

Each probe reads state through public APIs only (worker handles, JobQueue,
ModelRouter.status, an HTTP health URL, a config validator) and never acts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

from omnidesk_agent.self_healing.policy import RuntimeSignal

# Failures worth retrying once the dependency is back. Anything else stays
# dead-lettered for a human.
TRANSIENT_MARKERS = (
    "timeout", "timed out", "temporarily", "connection reset", "connection refused", "connecterror",
    "connectionerror", "remotedisconnected", "502", "503", "504", "429", "rate limit",
    "stale running job recovered",
)


def is_transient(error: Optional[str]) -> bool:
    text = str(error or "").lower()
    return any(marker in text for marker in TRANSIENT_MARKERS)


@dataclass
class WorkerHandle:
    name: str
    start: Callable[[], Any]
    stop: Callable[[], Any]
    is_alive: Callable[[], bool]


class WorkerRegistry:
    """Long-running workers the daemon registers so they can be restarted."""

    def __init__(self) -> None:
        self._handles: dict[str, WorkerHandle] = {}

    def register(self, name: str, *, start: Callable[[], Any], stop: Callable[[], Any], is_alive: Callable[[], bool]) -> None:
        self._handles[name] = WorkerHandle(name, start, stop, is_alive)

    def get(self, name: str) -> Optional[WorkerHandle]:
        return self._handles.get(name)

    def handles(self) -> list[WorkerHandle]:
        return list(self._handles.values())


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
            signals.append(RuntimeSignal(component=f"worker:{handle.name}", health_ok=alive))
        return signals


class JobQueueProbe:
    name = "job_queue"

    def __init__(self, queue: Any, *, lease_seconds: int = 300, dependency_healthy: Optional[Callable[[], bool]] = None, clock: Callable[[], float] = time.time):
        self.queue = queue
        self.lease_seconds = int(lease_seconds)
        self.dependency_healthy = dependency_healthy or (lambda: True)
        self.clock = clock

    async def collect(self) -> list[RuntimeSignal]:
        running, dead = await asyncio.gather(
            asyncio.to_thread(self.queue.list, status="running", limit=200),
            asyncio.to_thread(self.queue.list_dead_letters, limit=200),
        )
        cutoff = self.clock() - self.lease_seconds
        stale = [job for job in running if job.get("locked_at") is not None and float(job["locked_at"]) <= cutoff]
        transient = [job for job in dead if is_transient(job.get("last_error"))]
        try:
            healthy = bool(self.dependency_healthy())
        except Exception:
            healthy = False
        return [
            RuntimeSignal(component="queue:stale_jobs", health_ok=not stale, metadata={"stale_count": len(stale)}),
            RuntimeSignal(
                component="queue:dead_letters",
                health_ok=not transient and len(dead) < 200,
                metadata={
                    "transient_count": len(transient),
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
    """Last-known-good copies of a config file, written atomically."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def save_known_good(self, config_path: Path) -> str:
        data = Path(config_path).read_bytes()
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
        if config_path.exists():
            rejected = self.root / f"rejected-{int(time.time())}-{hashlib.sha256(config_path.read_bytes()).hexdigest()[:8]}.config"
            shutil.copy2(config_path, rejected)
        _atomic_write(config_path, data)
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


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
