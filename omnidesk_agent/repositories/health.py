from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from typing import Any

from omnidesk_agent.repositories.base import RepositoryFactory


@dataclass(frozen=True)
class RuntimeStorageHealth:
    ok: bool
    backend: str
    multi_instance_safe: bool
    transactional_outbox: bool
    advisory_locks: bool
    row_level_locking: bool
    latency_seconds: float
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_repository_factory(factory: RepositoryFactory, *, require_multi_instance_safe: bool = False, live_write: bool = False) -> RuntimeStorageHealth:
    """Validate runtime storage capabilities and optionally exercise outbox IO.

    `live_write=True` performs a tiny transactional outbox round-trip. It is used
    by readiness drills, not by hot request paths.
    """

    started = time.time()
    caps = factory.capabilities
    error: str | None = None
    ok = True
    if require_multi_instance_safe and not caps.multi_instance_safe:
        ok = False
        error = "multi-instance-safe storage is required but backend does not provide it"
    if ok and live_write:
        try:
            repo = factory.transactional_outbox()
            event_id = repo.enqueue(topic="runtime.storage.health", payload={"ts": started}, dedupe_key=f"runtime-health-{started}")
            claimed = repo.claim_batch(limit=1, lease_seconds=5)
            if not any(item.get("id") == event_id for item in claimed):
                raise RuntimeError("transactional outbox health event was not claimable")
            repo.mark_done(event_id)
        except Exception as exc:  # pragma: no cover - exercised by integration drills
            ok = False
            error = str(exc)
    return RuntimeStorageHealth(
        ok=ok,
        backend=caps.backend,
        multi_instance_safe=caps.multi_instance_safe,
        transactional_outbox=caps.transactional_outbox,
        advisory_locks=caps.advisory_locks,
        row_level_locking=caps.row_level_locking,
        latency_seconds=time.time() - started,
        error=error,
    )


def render_storage_health_json(health: RuntimeStorageHealth) -> str:
    return json.dumps(health.to_dict(), ensure_ascii=False, sort_keys=True)
