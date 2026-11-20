"""Retention for the PostgreSQL core runtime state (``omnidesk_core_state``).

Runs with the AppSync retention worker (one elected replica at a time):

- structured memories past their ``expires_at`` go through the memory store's
  own audited purge, oldest first, in batches;
- side-effect idempotency rows past their ``expires_at`` are deleted;
- the model response cache and the model usage records older than their
  window are deleted (``OMNIDESK_CORE_RETENTION_LLM_CACHE_DAYS``, default 7;
  ``OMNIDESK_CORE_RETENTION_LLM_USAGE_DAYS``, default 90, which covers the
  monthly budget window).
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

DAY = 86_400.0


def _days(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number of days") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be at least 1")
    return value


def run_core_state_retention(runtime_state: Any, *, now: Optional[float] = None, batch: int = 1000, max_batches: int = 100) -> dict[str, Any]:
    """Prune expired and aged rows of the core state; returns counts per kind."""
    now = time.time() if now is None else float(now)
    state = runtime_state.state
    report: dict[str, Any] = {}

    memory = runtime_state.memory_store()
    purged = 0
    for _ in range(max_batches):
        result = memory.purge_expired(dry_run=False, now=now, limit=batch)
        purged += int(result.get("deleted_count") or 0)
        if int(result.get("candidate_count") or 0) < batch:
            break
    report["structured_experiences"] = {"deleted": purged}

    def drain(delete: Any) -> int:
        total = 0
        for _ in range(max_batches):
            count = int(delete())
            total += count
            if count < batch:
                break
        return total

    report["side_effect_idempotency"] = {"deleted": drain(lambda: state.delete_expired("side_effect_idempotency", before=now, limit=batch))}
    cache_days = _days("OMNIDESK_CORE_RETENTION_LLM_CACHE_DAYS", 7)
    usage_days = _days("OMNIDESK_CORE_RETENTION_LLM_USAGE_DAYS", 90)
    report["llm_cache"] = {"deleted": drain(lambda: state.delete_stale("llm_cache", stale_before=now - cache_days * DAY, limit=batch)), "days": cache_days}
    report["llm_usage"] = {"deleted": drain(lambda: state.delete_stale("llm_usage", stale_before=now - usage_days * DAY, limit=batch)), "days": usage_days}
    return report
