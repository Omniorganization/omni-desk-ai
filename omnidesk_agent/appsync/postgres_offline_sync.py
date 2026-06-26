from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from omnidesk_agent.appsync.postgres_store import PostgresAppSyncStore
from omnidesk_agent.appsync.store import LocalInboxRecord, LocalOutboxRecord, SyncConflictRecord, SyncCursorRecord


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except Exception:
            return {}
    return {}


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class DurablePostgresAppSyncStore(PostgresAppSyncStore):
    """PostgreSQL AppSync store with durable offline sync state."""

    def _load(self) -> None:
        super()._load()
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                ns = self.namespace
                cur.execute(
                    """
                    SELECT operation_id, operation_type, payload, actor, organization_id, device_id,
                           idempotency_key, payload_hash, conflict_policy, status, retry_count,
                           next_retry_at, remote_seq, last_error, created_at, updated_at
                    FROM omnidesk_appsync_local_outbox
                    WHERE namespace=%s
                    """,
                    (ns,),
                )
                self.local_outbox = {
                    r[0]: LocalOutboxRecord(
                        operation_id=r[0],
                        operation_type=r[1],
                        payload=_json_dict(r[2]),
                        actor=r[3],
                        organization_id=r[4],
                        device_id=r[5],
                        idempotency_key=r[6],
                        payload_hash=r[7],
                        conflict_policy=r[8],
                        status=r[9],
                        retry_count=int(r[10] or 0),
                        next_retry_at=_float_or_none(r[11]),
                        remote_seq=int(r[12]) if r[12] is not None else None,
                        last_error=r[13],
                        created_at=float(r[14]),
                        updated_at=float(r[15]),
                    )
                    for r in cur.fetchall()
                }

                cur.execute(
                    """
                    SELECT remote_event_id, event_type, payload, actor, organization_id,
                           source_device_id, idempotency_key, payload_hash, status, received_at
                    FROM omnidesk_appsync_local_inbox
                    WHERE namespace=%s
                    """,
                    (ns,),
                )
                self.local_inbox = {
                    r[0]: LocalInboxRecord(
                        remote_event_id=r[0],
                        event_type=r[1],
                        payload=_json_dict(r[2]),
                        actor=r[3],
                        organization_id=r[4],
                        source_device_id=r[5],
                        idempotency_key=r[6],
                        payload_hash=r[7],
                        status=r[8],
                        received_at=float(r[9]),
                    )
                    for r in cur.fetchall()
                }

                cur.execute(
                    """
                    SELECT cursor_id, remote, actor, organization_id, device_id, since_seq, updated_at
                    FROM omnidesk_appsync_sync_cursors
                    WHERE namespace=%s
                    """,
                    (ns,),
                )
                self.sync_cursors = {
                    r[0]: SyncCursorRecord(
                        cursor_id=r[0],
                        remote=r[1],
                        actor=r[2],
                        organization_id=r[3],
                        device_id=r[4],
                        since_seq=int(r[5] or 0),
                        updated_at=float(r[6]),
                    )
                    for r in cur.fetchall()
                }

                cur.execute(
                    """
                    SELECT conflict_id, operation_id, remote_event_id, conflict_type, strategy,
                           status, actor, organization_id, local_payload, remote_payload,
                           created_at, resolved_at
                    FROM omnidesk_appsync_sync_conflicts
                    WHERE namespace=%s
                    """,
                    (ns,),
                )
                self.sync_conflicts = {
                    r[0]: SyncConflictRecord(
                        conflict_id=r[0],
                        operation_id=r[1],
                        remote_event_id=r[2],
                        conflict_type=r[3],
                        strategy=r[4],
                        status=r[5],
                        actor=r[6],
                        organization_id=r[7],
                        local_payload=_json_dict(r[8]),
                        remote_payload=_json_dict(r[9]),
                        created_at=float(r[10]),
                        resolved_at=_float_or_none(r[11]),
                    )
                    for r in cur.fetchall()
                }

                cur.execute(
                    """
                    SELECT seq, event, payload, organization_id, created_at
                    FROM omnidesk_appsync_operation_log
                    WHERE namespace=%s
                    ORDER BY seq ASC
                    """,
                    (ns,),
                )
                self.operation_log = [
                    {
                        "seq": int(r[0]),
                        "ts": float(r[4]),
                        "event": r[1],
                        "payload": {**_json_dict(r[2]), "organization_id": r[3]},
                    }
                    for r in cur.fetchall()
                ][-2000:]

    def _mirror_normalized(self, cur: Any, Jsonb: Any) -> None:
        super()._mirror_normalized(cur, Jsonb)
        ns = self.namespace
        for item in self.local_outbox.values():
            v = asdict(item)
            cur.execute(
                """
                INSERT INTO omnidesk_appsync_local_outbox(
                    namespace, organization_id, operation_id, operation_type, payload, actor,
                    device_id, idempotency_key, payload_hash, conflict_policy, status,
                    retry_count, next_retry_at, remote_seq, last_error, created_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace, operation_id) DO UPDATE SET
                    organization_id=EXCLUDED.organization_id,
                    operation_type=EXCLUDED.operation_type,
                    payload=EXCLUDED.payload,
                    actor=EXCLUDED.actor,
                    device_id=EXCLUDED.device_id,
                    idempotency_key=EXCLUDED.idempotency_key,
                    payload_hash=EXCLUDED.payload_hash,
                    conflict_policy=EXCLUDED.conflict_policy,
                    status=EXCLUDED.status,
                    retry_count=EXCLUDED.retry_count,
                    next_retry_at=EXCLUDED.next_retry_at,
                    remote_seq=EXCLUDED.remote_seq,
                    last_error=EXCLUDED.last_error,
                    updated_at=EXCLUDED.updated_at
                """,
                (
                    ns,
                    v["organization_id"],
                    v["operation_id"],
                    v["operation_type"],
                    Jsonb(v["payload"]),
                    v["actor"],
                    v.get("device_id"),
                    v.get("idempotency_key"),
                    v.get("payload_hash", ""),
                    v.get("conflict_policy", "manual-review"),
                    v.get("status", "pending"),
                    v.get("retry_count", 0),
                    v.get("next_retry_at"),
                    v.get("remote_seq"),
                    v.get("last_error"),
                    v.get("created_at", 0.0),
                    v.get("updated_at", 0.0),
                ),
            )

        for item in self.local_inbox.values():
            v = asdict(item)
            cur.execute(
                """
                INSERT INTO omnidesk_appsync_local_inbox(
                    namespace, organization_id, remote_event_id, event_type, payload,
                    actor, source_device_id, idempotency_key, payload_hash, status, received_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace, remote_event_id) DO UPDATE SET
                    organization_id=EXCLUDED.organization_id,
                    event_type=EXCLUDED.event_type,
                    payload=EXCLUDED.payload,
                    actor=EXCLUDED.actor,
                    source_device_id=EXCLUDED.source_device_id,
                    idempotency_key=EXCLUDED.idempotency_key,
                    payload_hash=EXCLUDED.payload_hash,
                    status=EXCLUDED.status
                """,
                (
                    ns,
                    v["organization_id"],
                    v["remote_event_id"],
                    v["event_type"],
                    Jsonb(v["payload"]),
                    v["actor"],
                    v.get("source_device_id"),
                    v.get("idempotency_key"),
                    v.get("payload_hash", ""),
                    v.get("status", "received"),
                    v.get("received_at", 0.0),
                ),
            )

        for item in self.sync_cursors.values():
            v = asdict(item)
            cur.execute(
                """
                INSERT INTO omnidesk_appsync_sync_cursors(
                    namespace, organization_id, cursor_id, remote, actor, device_id, since_seq, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace, cursor_id) DO UPDATE SET
                    organization_id=EXCLUDED.organization_id,
                    remote=EXCLUDED.remote,
                    actor=EXCLUDED.actor,
                    device_id=EXCLUDED.device_id,
                    since_seq=EXCLUDED.since_seq,
                    updated_at=EXCLUDED.updated_at
                """,
                (ns, v["organization_id"], v["cursor_id"], v["remote"], v["actor"], v.get("device_id"), v.get("since_seq", 0), v.get("updated_at", 0.0)),
            )

        for item in self.sync_conflicts.values():
            v = asdict(item)
            cur.execute(
                """
                INSERT INTO omnidesk_appsync_sync_conflicts(
                    namespace, organization_id, conflict_id, operation_id, remote_event_id,
                    conflict_type, strategy, status, actor, local_payload, remote_payload,
                    created_at, resolved_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace, conflict_id) DO UPDATE SET
                    organization_id=EXCLUDED.organization_id,
                    operation_id=EXCLUDED.operation_id,
                    remote_event_id=EXCLUDED.remote_event_id,
                    conflict_type=EXCLUDED.conflict_type,
                    strategy=EXCLUDED.strategy,
                    status=EXCLUDED.status,
                    actor=EXCLUDED.actor,
                    local_payload=EXCLUDED.local_payload,
                    remote_payload=EXCLUDED.remote_payload,
                    resolved_at=EXCLUDED.resolved_at
                """,
                (
                    ns,
                    v["organization_id"],
                    v["conflict_id"],
                    v["operation_id"],
                    v.get("remote_event_id"),
                    v["conflict_type"],
                    v.get("strategy", "manual-review"),
                    v.get("status", "open"),
                    v.get("actor", "system"),
                    Jsonb(v.get("local_payload") or {}),
                    Jsonb(v.get("remote_payload") or {}),
                    v.get("created_at", 0.0),
                    v.get("resolved_at"),
                ),
            )

        for item in self.operation_log[-2000:]:
            payload = dict(item.get("payload") or {})
            organization_id = str(payload.get("organization_id") or "org_default")
            cur.execute(
                """
                INSERT INTO omnidesk_appsync_operation_log(namespace, organization_id, seq, event, payload, created_at)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace, seq, event) DO UPDATE SET payload=EXCLUDED.payload
                """,
                (ns, organization_id, int(item.get("seq", 0)), str(item.get("event") or "unknown"), Jsonb(payload), float(item.get("ts") or 0.0)),
            )
