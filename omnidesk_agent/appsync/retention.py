"""Retention for AppSync tables on PostgreSQL.

Without it, sync events, idempotency responses, push and offline-sync records
grow forever, and every one of them is personal or operational data. Each rule
deletes rows past their age in short batched transactions, so a run never holds
locks for long or trips the statement timeout.

Sync events are pruned by sequence number. The pruning watermark
(``omnidesk_appsync_event_counters.pruned_through``) is committed *before* the
events are deleted, so a reader whose cursor is below it is told to
re-bootstrap (``reset_required``) instead of silently skipping the gap.

Conversations and messages are user data and are not pruned by age.

Run in-process (``RetentionWorker``, started by the AppSync routes on
PostgreSQL) or from cron::

    python -m omnidesk_agent.appsync.retention --namespace production [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

DAY = 86_400.0


@dataclass(frozen=True)
class RetentionRule:
    """Delete rows of ``table`` whose ``timestamp_column`` is older than ``days``.

    ``condition`` is a fixed SQL fragment from this module, never input.
    """

    name: str
    table: str
    timestamp_column: str
    days: float
    condition: str = ""


def _days(name: str, default: float) -> float:
    raw = os.getenv(f"OMNIDESK_APPSYNC_RETENTION_{name.upper()}_DAYS", "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"OMNIDESK_APPSYNC_RETENTION_{name.upper()}_DAYS must be a number of days") from exc
    if value < 1:
        raise RuntimeError(f"OMNIDESK_APPSYNC_RETENTION_{name.upper()}_DAYS must be at least 1")
    return value


def sync_event_retention_days() -> float:
    return _days("sync_events", 14)


def default_rules() -> tuple[RetentionRule, ...]:
    """Rows nothing reads after these ages. Completed enrollments are kept: they
    are the record of which enrollment bound a device's key."""
    return (
        RetentionRule("idempotency_keys", "omnidesk_appsync_idempotency_keys", "created_at", _days("idempotency_keys", 7)),
        RetentionRule("push_outbox", "omnidesk_appsync_push_outbox", "updated_at", _days("push_outbox", 30), "status IN ('sent', 'failed')"),
        RetentionRule("notifications", "omnidesk_appsync_notifications", "created_at", _days("notifications", 90)),
        RetentionRule("device_challenges", "omnidesk_appsync_device_challenges", "created_at", _days("device_challenges", 30)),
        RetentionRule("device_enrollments", "omnidesk_appsync_device_enrollments", "created_at", _days("device_enrollments", 30), "status IN ('pending', 'expired', 'revoked')"),
        RetentionRule("local_inbox", "omnidesk_appsync_local_inbox", "received_at", _days("local_inbox", 30)),
        RetentionRule("local_outbox", "omnidesk_appsync_local_outbox", "updated_at", _days("local_outbox", 30), "status = 'synced'"),
        RetentionRule("operation_log", "omnidesk_appsync_operation_log", "created_at", _days("operation_log", 30)),
        RetentionRule("sync_conflicts", "omnidesk_appsync_sync_conflicts", "resolved_at", _days("sync_conflicts", 90), "status = 'resolved'"),
        # Stream events go with their request (ON DELETE CASCADE).
        RetentionRule("chat_requests", "omnidesk_appsync_chat_requests", "updated_at", _days("chat_requests", 30), "status IN ('completed', 'failed', 'interrupted')"),
    )


def _lock_key(namespace: str) -> int:
    import hashlib

    digest = hashlib.sha256(f"omnidesk-appsync-retention:{namespace}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


# The first event young enough to keep; everything before it is pruned.
FIRST_KEPT_EVENT_SQL = """
SELECT seq FROM omnidesk_appsync_sync_events
WHERE namespace = %(namespace)s AND created_at >= %(cutoff)s
ORDER BY seq ASC LIMIT 1
"""

SYNC_HEAD_FOR_RETENTION_SQL = """
SELECT GREATEST(
    COALESCE((SELECT seq FROM omnidesk_appsync_event_counters WHERE namespace = %(namespace)s), 0),
    COALESCE((SELECT MAX(seq) FROM omnidesk_appsync_sync_events WHERE namespace = %(namespace)s), 0)
)
"""

# Committed before any event is deleted (see the module docstring).
RAISE_WATERMARK_SQL = """
INSERT INTO omnidesk_appsync_event_counters AS counter (namespace, seq, pruned_through)
VALUES (%(namespace)s, %(head)s, %(cut)s)
ON CONFLICT (namespace) DO UPDATE SET pruned_through = GREATEST(counter.pruned_through, EXCLUDED.pruned_through)
"""

DELETE_EVENTS_SQL = """
DELETE FROM omnidesk_appsync_sync_events
WHERE ctid IN (
    SELECT ctid FROM omnidesk_appsync_sync_events
    WHERE namespace = %(namespace)s AND seq <= %(cut)s
    ORDER BY seq ASC
    LIMIT %(batch)s
)
"""

DELETE_EXPIRED_NONCES_SQL = """
DELETE FROM omnidesk_appsync_device_request_nonces
WHERE ctid IN (
    SELECT ctid FROM omnidesk_appsync_device_request_nonces
    WHERE namespace = %(namespace)s AND expires_at <= %(now)s
    LIMIT %(batch)s
)
"""


def _rule_delete_sql(rule: RetentionRule, *, count_only: bool) -> Any:
    from psycopg import sql  # type: ignore

    condition = sql.SQL(" AND ") + sql.SQL(rule.condition) if rule.condition else sql.SQL("")
    selection = sql.SQL("SELECT ctid FROM {table} WHERE namespace = %(namespace)s AND {column} < %(cutoff)s{condition} LIMIT %(batch)s").format(
        table=sql.Identifier(rule.table), column=sql.Identifier(rule.timestamp_column), condition=condition
    )
    if count_only:
        return sql.SQL("SELECT COUNT(*) FROM ({selection}) candidates").format(selection=selection)
    return sql.SQL("DELETE FROM {table} WHERE ctid IN ({selection})").format(table=sql.Identifier(rule.table), selection=selection)


def _delete_batches(store: Any, statement: Any, params: dict[str, Any], *, batch: int, max_batches: int) -> int:
    deleted = 0
    for _ in range(max_batches):
        with store._connect() as conn, conn.cursor() as cur:
            cur.execute(statement, {**params, "batch": batch})
            count = int(cur.rowcount or 0)
        deleted += count
        if count < batch:
            break
    return deleted


def prune_sync_events(store: Any, *, now: float, days: float, batch: int = 5000, max_batches: int = 200, dry_run: bool = False) -> dict[str, Any]:
    cutoff = now - days * DAY
    namespace = store.namespace
    with store._connect() as conn, conn.cursor() as cur:
        cur.execute(SYNC_HEAD_FOR_RETENTION_SQL, {"namespace": namespace})
        head = int(cur.fetchone()[0])
        cur.execute(FIRST_KEPT_EVENT_SQL, {"namespace": namespace, "cutoff": cutoff})
        row = cur.fetchone()
    cut = int(row[0]) - 1 if row else head
    if cut <= 0:
        return {"deleted": 0, "pruned_through": 0}
    if dry_run:
        with store._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM omnidesk_appsync_sync_events WHERE namespace = %s AND seq <= %s", (namespace, cut))
            return {"would_delete": int(cur.fetchone()[0]), "pruned_through": cut}
    with store._connect() as conn, conn.cursor() as cur:
        cur.execute(RAISE_WATERMARK_SQL, {"namespace": namespace, "head": head, "cut": cut})
    deleted = _delete_batches(store, DELETE_EVENTS_SQL, {"namespace": namespace, "cut": cut}, batch=batch, max_batches=max_batches)
    return {"deleted": deleted, "pruned_through": cut}


def run_retention(
    store: Any,
    *,
    now: Optional[float] = None,
    rules: Optional[tuple[RetentionRule, ...]] = None,
    sync_event_days: Optional[float] = None,
    batch: int = 5000,
    max_batches: int = 200,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Prune one namespace. Only one replica runs at a time; the others skip."""
    import psycopg  # type: ignore

    now = time.time() if now is None else now
    report: dict[str, Any] = {"namespace": store.namespace, "started_at": now, "dry_run": dry_run, "tables": {}}
    pool = store._connection_pool
    with psycopg.connect(store.dsn, autocommit=True, **pool._connect_kwargs()) as election:
        locked = election.execute("SELECT pg_try_advisory_lock(%s)", (_lock_key(store.namespace),)).fetchone()
        if not locked or not locked[0]:
            report["skipped"] = "another replica holds the retention lock"
            return report
        try:
            tables: dict[str, Any] = report["tables"]
            tables["sync_events"] = prune_sync_events(
                store, now=now, days=sync_event_retention_days() if sync_event_days is None else sync_event_days,
                batch=batch, max_batches=max_batches, dry_run=dry_run,
            )
            if dry_run:
                with store._connect() as conn, conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM omnidesk_appsync_device_request_nonces WHERE namespace = %s AND expires_at <= %s", (store.namespace, now))
                    tables["device_request_nonces"] = {"would_delete": int(cur.fetchone()[0])}
            else:
                tables["device_request_nonces"] = {"deleted": _delete_batches(store, DELETE_EXPIRED_NONCES_SQL, {"namespace": store.namespace, "now": now}, batch=batch, max_batches=max_batches)}
            for rule in rules if rules is not None else default_rules():
                params = {"namespace": store.namespace, "cutoff": now - rule.days * DAY}
                if dry_run:
                    with store._connect() as conn, conn.cursor() as cur:
                        cur.execute(_rule_delete_sql(rule, count_only=True), {**params, "batch": batch * max_batches})
                        tables[rule.name] = {"would_delete": int(cur.fetchone()[0]), "days": rule.days}
                else:
                    tables[rule.name] = {"deleted": _delete_batches(store, _rule_delete_sql(rule, count_only=False), params, batch=batch, max_batches=max_batches), "days": rule.days}
        finally:
            election.execute("SELECT pg_advisory_unlock(%s)", (_lock_key(store.namespace),))
    report["finished_at"] = time.time()
    return report


class RetentionWorker:
    """Runs :func:`run_retention` every ``interval_seconds`` on a daemon thread."""

    def __init__(self, store: Any, *, interval_seconds: float, metrics: Any = None) -> None:
        self.store = store
        self.interval_seconds = max(60.0, float(interval_seconds))
        self.metrics = metrics
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_report: Optional[dict[str, Any]] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="omnidesk-appsync-retention", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        # Replicas started together should not all scan at the same moment.
        delay = random.uniform(30.0, min(self.interval_seconds, 600.0))  # nosec B311 - scheduling jitter, not security
        while not self._stop.wait(delay):
            try:
                report = run_retention(self.store)
                self.last_report = report
                if self.metrics is not None:
                    for table, outcome in report.get("tables", {}).items():
                        self.metrics.inc("omnidesk_appsync_retention_deleted_total", value=int(outcome.get("deleted", 0)), table=table)
            except Exception:
                logger.exception("AppSync retention run failed", extra={"namespace": getattr(self.store, "namespace", "")})
                if self.metrics is not None:
                    self.metrics.inc("omnidesk_appsync_retention_failures_total")
            delay = self.interval_seconds


def retention_interval_seconds() -> float:
    raw = os.getenv("OMNIDESK_APPSYNC_RETENTION_INTERVAL_SECONDS", "").strip()
    if not raw:
        return 3600.0
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError("OMNIDESK_APPSYNC_RETENTION_INTERVAL_SECONDS must be a number of seconds") from exc


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Prune AppSync PostgreSQL tables past their retention.")
    parser.add_argument("--dsn-env", default="OMNIDESK_APPSYNC_POSTGRES_DSN")
    parser.add_argument("--namespace", default="production")
    parser.add_argument("--dry-run", action="store_true", help="Count rows that would be deleted; delete nothing.")
    args = parser.parse_args(argv)
    dsn = os.getenv(args.dsn_env, "").strip()
    if not dsn:
        raise RuntimeError(f"Missing PostgreSQL DSN environment variable: {args.dsn_env}")
    from omnidesk_agent.appsync.migrated_postgres_store import MigratedMultiInstancePostgresAppSyncStore

    store = MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=args.namespace, pool_size=2)
    try:
        report = run_retention(store, dry_run=args.dry_run)
    finally:
        store.close()
    print(json.dumps(report, sort_keys=True))
    return 0 if "skipped" not in report else 3


if __name__ == "__main__":
    raise SystemExit(main())
