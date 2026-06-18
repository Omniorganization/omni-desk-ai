from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from omnidesk_agent.appsync.store import (
    AppSyncStore,
    ApprovalRecord,
    ConversationRecord,
    DeviceEnrollmentRecord,
    DeviceRecord,
    MessageRecord,
    NotificationRecord,
    Organization,
    RuntimeStatusRecord,
    TaskRecord,
    UserProfile,
    PushOutboxRecord,
    DeviceChallengeRecord,
)


NORMALIZED_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS omnidesk_appsync_organizations (
    organization_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS omnidesk_appsync_users (
    namespace TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(namespace, user_id)
);
CREATE TABLE IF NOT EXISTS omnidesk_appsync_devices (
    namespace TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    device_type TEXT NOT NULL,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    actor TEXT NOT NULL,
    push_token TEXT,
    public_key TEXT,
    token_hash TEXT,
    credential_status TEXT NOT NULL DEFAULT 'pending',
    trust_level TEXT NOT NULL DEFAULT 'unverified',
    revoked_at DOUBLE PRECISION,
    last_challenge_nonce_hash TEXT,
    last_challenge_expires_at DOUBLE PRECISION,
    capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
    online BOOLEAN NOT NULL DEFAULT true,
    last_seen_at DOUBLE PRECISION NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(namespace, device_id)
);
CREATE TABLE IF NOT EXISTS omnidesk_appsync_conversations (
    namespace TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    title TEXT NOT NULL,
    actor TEXT NOT NULL,
    source_device_id TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(namespace, conversation_id)
);
CREATE TABLE IF NOT EXISTS omnidesk_appsync_messages (
    namespace TEXT NOT NULL,
    message_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    actor TEXT NOT NULL,
    source_device_id TEXT,
    task_id TEXT,
    model_provider TEXT,
    model_name TEXT,
    model_profile TEXT,
    usage JSONB NOT NULL DEFAULT '{}'::jsonb,
    trace_id TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(namespace, message_id)
);
ALTER TABLE omnidesk_appsync_messages ADD COLUMN IF NOT EXISTS model_provider TEXT;
ALTER TABLE omnidesk_appsync_messages ADD COLUMN IF NOT EXISTS model_name TEXT;
ALTER TABLE omnidesk_appsync_messages ADD COLUMN IF NOT EXISTS model_profile TEXT;
ALTER TABLE omnidesk_appsync_messages ADD COLUMN IF NOT EXISTS usage JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE omnidesk_appsync_messages ADD COLUMN IF NOT EXISTS trace_id TEXT;
CREATE TABLE IF NOT EXISTS omnidesk_appsync_tasks (
    namespace TEXT NOT NULL,
    task_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    title TEXT NOT NULL,
    actor TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    status TEXT NOT NULL,
    assigned_runtime_device_id TEXT,
    requires_desktop_runtime BOOLEAN NOT NULL DEFAULT false,
    approval_id TEXT,
    result_summary TEXT,
    idempotency_key TEXT,
    claimed_by_device_id TEXT,
    lease_expires_at DOUBLE PRECISION,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(namespace, task_id)
);
CREATE INDEX IF NOT EXISTS omnidesk_appsync_tasks_claim_idx
    ON omnidesk_appsync_tasks(namespace, requires_desktop_runtime, status, lease_expires_at, created_at);
CREATE TABLE IF NOT EXISTS omnidesk_appsync_approvals (
    namespace TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    risk TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL,
    decided_by TEXT,
    decision_reason TEXT,
    decision_idempotency_key TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    decided_at DOUBLE PRECISION,
    expires_at DOUBLE PRECISION,
    PRIMARY KEY(namespace, approval_id)
);
CREATE INDEX IF NOT EXISTS omnidesk_appsync_approvals_pending_idx
    ON omnidesk_appsync_approvals(namespace, status, expires_at);
CREATE TABLE IF NOT EXISTS omnidesk_appsync_notifications (
    namespace TEXT NOT NULL,
    notification_id TEXT NOT NULL,
    audience TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    related_id TEXT,
    read BOOLEAN NOT NULL DEFAULT false,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(namespace, notification_id)
);
CREATE TABLE IF NOT EXISTS omnidesk_appsync_runtime_status (
    namespace TEXT NOT NULL,
    device_id TEXT NOT NULL,
    status TEXT NOT NULL,
    version TEXT,
    hostname TEXT,
    active_task_id TEXT,
    capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
    last_heartbeat_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(namespace, device_id)
);
CREATE TABLE IF NOT EXISTS omnidesk_appsync_device_enrollments (
    namespace TEXT NOT NULL,
    enrollment_id TEXT NOT NULL,
    device_type TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    pairing_code_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    device_id TEXT,
    public_key TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    completed_at DOUBLE PRECISION,
    PRIMARY KEY(namespace, enrollment_id)
);
CREATE TABLE IF NOT EXISTS omnidesk_appsync_idempotency_keys (
    namespace TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    response JSONB NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(namespace, organization_id, actor, endpoint, key)
);
CREATE TABLE IF NOT EXISTS omnidesk_appsync_sync_events (
    namespace TEXT NOT NULL,
    seq BIGINT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(namespace, seq)
);
CREATE TABLE IF NOT EXISTS omnidesk_appsync_task_leases (
    namespace TEXT NOT NULL,
    task_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    lease_expires_at DOUBLE PRECISION NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(namespace, task_id)
);
CREATE TABLE IF NOT EXISTS omnidesk_appsync_push_outbox (
    namespace TEXT NOT NULL, push_id TEXT NOT NULL, device_id TEXT NOT NULL, platform TEXT NOT NULL, push_token TEXT, audience TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, related_id TEXT, status TEXT NOT NULL DEFAULT 'pending', attempt_count INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL, PRIMARY KEY(namespace, push_id)
);
CREATE INDEX IF NOT EXISTS omnidesk_appsync_push_pending_idx ON omnidesk_appsync_push_outbox(namespace, status, created_at);
CREATE TABLE IF NOT EXISTS omnidesk_appsync_device_challenges (
    namespace TEXT NOT NULL, challenge_id TEXT NOT NULL, enrollment_id TEXT NOT NULL, device_id TEXT NOT NULL, nonce_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL, verified_at DOUBLE PRECISION, PRIMARY KEY(namespace, challenge_id)
);
"""


class PostgresAppSyncStore(AppSyncStore):
    """PostgreSQL-backed AppSync store using normalized database tables.

    JSON storage remains available only for local/dev deployments. In production,
    this store treats PostgreSQL normalized tables as the durable source of truth:
    writes are mirrored through transactional upserts, idempotency uses unique
    database constraints, task claiming uses row locks, and startup hydration reads
    from the database tables rather than a compact file/state payload.
    """

    def __init__(self, dsn: str, namespace: str = "default"):
        self.dsn = dsn
        self.namespace = namespace or "default"
        super().__init__(Path(".postgres-appsync-state.json"))

    def _connect(self):
        try:
            import psycopg  # type: ignore
        except ModuleNotFoundError as exc:  # pragma: no cover - exercised in enterprise env
            raise RuntimeError("Install omnidesk-agent[enterprise] or psycopg[binary] to use PostgreSQL AppSync") from exc
        return psycopg.connect(self.dsn)

    def _ensure_schema(self, conn: Any) -> None:
        with conn.cursor() as cur:
            cur.execute(NORMALIZED_SCHEMA_SQL)
        conn.commit()

    def _payload(self) -> dict[str, Any]:
        return {
            "seq": self._seq,
            "organization": asdict(self.organization),
            "users": {k: asdict(v) for k, v in self.users.items()},
            "devices": {k: asdict(v) for k, v in self.devices.items()},
            "conversations": {k: asdict(v) for k, v in self.conversations.items()},
            "messages": {k: asdict(v) for k, v in self.messages.items()},
            "tasks": {k: asdict(v) for k, v in self.tasks.items()},
            "approvals": {k: asdict(v) for k, v in self.approvals.items()},
            "notifications": {k: asdict(v) for k, v in self.notifications.items()},
            "runtimes": {k: asdict(v) for k, v in self.runtimes.items()},
            "device_enrollments": {k: asdict(v) for k, v in self.device_enrollments.items()},
            "push_outbox": {k: asdict(v) for k, v in self.push_outbox.items()},
            "device_challenges": {k: asdict(v) for k, v in self.device_challenges.items()},
            "events": self.events[-1000:],
            "idempotency": self.idempotency,
        }

    @staticmethod
    def _json_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
                if isinstance(decoded, list):
                    return [str(item) for item in decoded]
            except Exception:
                return []
        return []

    def _load(self) -> None:
        """Hydrate in-memory indexes from normalized PostgreSQL tables.

        Production deployments do not load a compact state document. The process
        cache is rebuilt from the transactional tables so restarts and additional
        gateway instances observe the same database-backed source of truth.
        """
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                ns = self.namespace
                cur.execute("SELECT organization_id, name, created_at FROM omnidesk_appsync_organizations WHERE namespace=%s ORDER BY created_at ASC LIMIT 1", (ns,))
                row = cur.fetchone()
                if row:
                    self.organization = Organization(organization_id=row[0], name=row[1], created_at=float(row[2]))

                cur.execute("SELECT organization_id, user_id, display_name, role, created_at, updated_at FROM omnidesk_appsync_users WHERE namespace=%s", (ns,))
                self.users = {r[1]: UserProfile(organization_id=r[0], user_id=r[1], display_name=r[2], role=r[3], created_at=float(r[4]), updated_at=float(r[5])) for r in cur.fetchall()}

                cur.execute("""
                    SELECT organization_id, device_id, device_type, name, platform, actor, push_token, public_key, token_hash, credential_status, trust_level, revoked_at, last_challenge_nonce_hash, last_challenge_expires_at, capabilities, online, last_seen_at, created_at, updated_at
                    FROM omnidesk_appsync_devices WHERE namespace=%s
                """, (ns,))
                self.devices = {r[1]: DeviceRecord(organization_id=r[0], device_id=r[1], device_type=r[2], name=r[3], platform=r[4], actor=r[5], push_token=r[6], public_key=r[7], token_hash=r[8], credential_status=r[9], trust_level=r[10], revoked_at=r[11], last_challenge_nonce_hash=r[12], last_challenge_expires_at=r[13], capabilities=self._json_list(r[14]), online=bool(r[15]), last_seen_at=float(r[16]), created_at=float(r[17]), updated_at=float(r[18])) for r in cur.fetchall()}

                cur.execute("SELECT conversation_id, title, actor, source_device_id, created_at, updated_at FROM omnidesk_appsync_conversations WHERE namespace=%s", (ns,))
                self.conversations = {r[0]: ConversationRecord(conversation_id=r[0], title=r[1], actor=r[2], source_device_id=r[3], created_at=float(r[4]), updated_at=float(r[5])) for r in cur.fetchall()}

                cur.execute("SELECT message_id, conversation_id, role, content, actor, source_device_id, task_id, model_provider, model_name, model_profile, usage, trace_id, created_at FROM omnidesk_appsync_messages WHERE namespace=%s", (ns,))
                self.messages = {r[0]: MessageRecord(message_id=r[0], conversation_id=r[1], role=r[2], content=r[3], actor=r[4], source_device_id=r[5], task_id=r[6], model_provider=r[7], model_name=r[8], model_profile=r[9], usage=r[10] if isinstance(r[10], dict) else json.loads(r[10] or "{}"), trace_id=r[11], created_at=float(r[12])) for r in cur.fetchall()}

                cur.execute("""
                    SELECT task_id, conversation_id, title, actor, organization_id, status, assigned_runtime_device_id, requires_desktop_runtime, approval_id, result_summary, idempotency_key, claimed_by_device_id, lease_expires_at, attempt_count, created_at, updated_at
                    FROM omnidesk_appsync_tasks WHERE namespace=%s
                """, (ns,))
                self.tasks = {r[0]: TaskRecord(task_id=r[0], conversation_id=r[1], title=r[2], actor=r[3], organization_id=r[4], status=r[5], assigned_runtime_device_id=r[6], requires_desktop_runtime=bool(r[7]), approval_id=r[8], result_summary=r[9], idempotency_key=r[10], claimed_by_device_id=r[11], lease_expires_at=r[12], attempt_count=int(r[13]), created_at=float(r[14]), updated_at=float(r[15])) for r in cur.fetchall()}

                cur.execute("SELECT approval_id, task_id, risk, action, reason, requested_by, status, decided_by, decision_reason, decision_idempotency_key, created_at, decided_at, expires_at FROM omnidesk_appsync_approvals WHERE namespace=%s", (ns,))
                self.approvals = {r[0]: ApprovalRecord(approval_id=r[0], task_id=r[1], risk=r[2], action=r[3], reason=r[4], requested_by=r[5], status=r[6], decided_by=r[7], decision_reason=r[8], decision_idempotency_key=r[9], created_at=float(r[10]), decided_at=r[11], expires_at=r[12]) for r in cur.fetchall()}

                cur.execute("SELECT notification_id, audience, title, body, event_type, actor, related_id, read, created_at FROM omnidesk_appsync_notifications WHERE namespace=%s", (ns,))
                self.notifications = {r[0]: NotificationRecord(notification_id=r[0], audience=r[1], title=r[2], body=r[3], event_type=r[4], actor=r[5], related_id=r[6], read=bool(r[7]), created_at=float(r[8])) for r in cur.fetchall()}

                cur.execute("SELECT device_id, status, version, hostname, active_task_id, capabilities, last_heartbeat_at FROM omnidesk_appsync_runtime_status WHERE namespace=%s", (ns,))
                self.runtimes = {r[0]: RuntimeStatusRecord(device_id=r[0], status=r[1], version=r[2], hostname=r[3], active_task_id=r[4], capabilities=self._json_list(r[5]), last_heartbeat_at=float(r[6])) for r in cur.fetchall()}

                cur.execute("SELECT enrollment_id, device_type, requested_by, pairing_code_hash, status, device_id, public_key, created_at, expires_at, completed_at FROM omnidesk_appsync_device_enrollments WHERE namespace=%s", (ns,))
                self.device_enrollments = {r[0]: DeviceEnrollmentRecord(enrollment_id=r[0], device_type=r[1], requested_by=r[2], pairing_code_hash=r[3], status=r[4], device_id=r[5], public_key=r[6], created_at=float(r[7]), expires_at=float(r[8]), completed_at=r[9]) for r in cur.fetchall()}

                cur.execute("SELECT push_id, device_id, platform, audience, title, body, related_id, status, attempt_count, last_error, created_at, updated_at FROM omnidesk_appsync_push_outbox WHERE namespace=%s", (ns,))
                self.push_outbox = {r[0]: PushOutboxRecord(push_id=r[0], device_id=r[1], platform=r[2], audience=r[3], title=r[4], body=r[5], related_id=r[6], status=r[7], attempt_count=int(r[8]), last_error=r[9], created_at=float(r[10]), updated_at=float(r[11])) for r in cur.fetchall()}

                cur.execute("SELECT challenge_id, enrollment_id, device_id, nonce_hash, status, created_at, expires_at, verified_at FROM omnidesk_appsync_device_challenges WHERE namespace=%s", (ns,))
                self.device_challenges = {r[0]: DeviceChallengeRecord(challenge_id=r[0], enrollment_id=r[1], device_id=r[2], nonce_hash=r[3], status=r[4], created_at=float(r[5]), expires_at=float(r[6]), verified_at=r[7]) for r in cur.fetchall()}

                cur.execute("SELECT seq, event_type, actor, payload, created_at FROM omnidesk_appsync_sync_events WHERE namespace=%s ORDER BY seq ASC", (ns,))
                self.events = [{"seq": int(r[0]), "event_type": r[1], "actor": r[2], "payload": r[3] if isinstance(r[3], dict) else json.loads(r[3]), "ts": float(r[4])} for r in cur.fetchall()][-1000:]
                self._seq = max([int(e.get("seq", 0)) for e in self.events] or [0])

                cur.execute("SELECT organization_id, actor, endpoint, key, payload_hash, response, created_at FROM omnidesk_appsync_idempotency_keys WHERE namespace=%s", (ns,))
                self.idempotency = {f"{r[0]}:{r[1] or 'unknown'}:{r[2]}:{r[3]}": {"organization_id": r[0], "actor": r[1], "endpoint": r[2], "key": r[3], "payload_hash": r[4], "response": r[5] if isinstance(r[5], dict) else json.loads(r[5]), "created_at": float(r[6])} for r in cur.fetchall()}

    def _persist(self) -> None:
        with self._connect() as conn:
            from psycopg.types.json import Jsonb  # type: ignore

            self._ensure_schema(conn)
            with conn.cursor() as cur:
                self._mirror_normalized(cur, Jsonb)
            conn.commit()

    def _mirror_normalized(self, cur: Any, Jsonb: Any) -> None:
        ns = self.namespace
        org = asdict(self.organization)
        cur.execute(
            """
            INSERT INTO omnidesk_appsync_organizations(namespace, organization_id, name, created_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT(organization_id) DO UPDATE SET name = EXCLUDED.name
            """,
            (ns, org["organization_id"], org["name"], org["created_at"]),
        )
        for item in self.users.values():
            v = asdict(item)
            cur.execute(
                """
                INSERT INTO omnidesk_appsync_users(namespace, organization_id, user_id, display_name, role, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace, user_id) DO UPDATE SET display_name=EXCLUDED.display_name, role=EXCLUDED.role, updated_at=EXCLUDED.updated_at
                """,
                (ns, v["organization_id"], v["user_id"], v["display_name"], v["role"], v["created_at"], v["updated_at"]),
            )
        for item in self.devices.values():
            v = asdict(item)
            cur.execute(
                """
                INSERT INTO omnidesk_appsync_devices(namespace, organization_id, device_id, device_type, name, platform, actor, push_token, public_key, token_hash, credential_status, trust_level, revoked_at, last_challenge_nonce_hash, last_challenge_expires_at, capabilities, online, last_seen_at, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace, device_id) DO UPDATE SET name=EXCLUDED.name, platform=EXCLUDED.platform, actor=EXCLUDED.actor, push_token=EXCLUDED.push_token, public_key=EXCLUDED.public_key, token_hash=EXCLUDED.token_hash, credential_status=EXCLUDED.credential_status, trust_level=EXCLUDED.trust_level, revoked_at=EXCLUDED.revoked_at, last_challenge_nonce_hash=EXCLUDED.last_challenge_nonce_hash, last_challenge_expires_at=EXCLUDED.last_challenge_expires_at, capabilities=EXCLUDED.capabilities, online=EXCLUDED.online, last_seen_at=EXCLUDED.last_seen_at, updated_at=EXCLUDED.updated_at
                """,
                (ns, v["organization_id"], v["device_id"], v["device_type"], v["name"], v["platform"], v["actor"], v.get("push_token"), v.get("public_key"), v.get("token_hash"), v.get("credential_status", "pending"), v.get("trust_level", "unverified"), v.get("revoked_at"), v.get("last_challenge_nonce_hash"), v.get("last_challenge_expires_at"), Jsonb(v.get("capabilities") or []), v["online"], v["last_seen_at"], v["created_at"], v["updated_at"]),
            )
        for item in self.conversations.values():
            v = asdict(item)
            cur.execute(
                """
                INSERT INTO omnidesk_appsync_conversations(namespace, conversation_id, title, actor, source_device_id, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace, conversation_id) DO UPDATE SET title=EXCLUDED.title, updated_at=EXCLUDED.updated_at
                """,
                (ns, v["conversation_id"], v["title"], v["actor"], v.get("source_device_id"), v["created_at"], v["updated_at"]),
            )
        for item in self.messages.values():
            v = asdict(item)
            cur.execute(
                """
                INSERT INTO omnidesk_appsync_messages(namespace, message_id, conversation_id, role, content, actor, source_device_id, task_id, model_provider, model_name, model_profile, usage, trace_id, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace, message_id) DO NOTHING
                """,
                (ns, v["message_id"], v["conversation_id"], v["role"], v["content"], v["actor"], v.get("source_device_id"), v.get("task_id"), v.get("model_provider"), v.get("model_name"), v.get("model_profile"), Jsonb(v.get("usage") or {}), v.get("trace_id"), v["created_at"]),
            )
        for item in self.tasks.values():
            v = asdict(item)
            cur.execute(
                """
                INSERT INTO omnidesk_appsync_tasks(namespace, task_id, conversation_id, title, actor, organization_id, status, assigned_runtime_device_id, requires_desktop_runtime, approval_id, result_summary, idempotency_key, claimed_by_device_id, lease_expires_at, attempt_count, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace, task_id) DO UPDATE SET status=EXCLUDED.status, assigned_runtime_device_id=EXCLUDED.assigned_runtime_device_id, approval_id=EXCLUDED.approval_id, result_summary=EXCLUDED.result_summary, claimed_by_device_id=EXCLUDED.claimed_by_device_id, lease_expires_at=EXCLUDED.lease_expires_at, attempt_count=EXCLUDED.attempt_count, updated_at=EXCLUDED.updated_at
                """,
                (ns, v["task_id"], v["conversation_id"], v["title"], v["actor"], v["organization_id"], v["status"], v.get("assigned_runtime_device_id"), v["requires_desktop_runtime"], v.get("approval_id"), v.get("result_summary"), v.get("idempotency_key"), v.get("claimed_by_device_id"), v.get("lease_expires_at"), v["attempt_count"], v["created_at"], v["updated_at"]),
            )
            if v.get("claimed_by_device_id") and v.get("lease_expires_at"):
                cur.execute(
                    """
                    INSERT INTO omnidesk_appsync_task_leases(namespace, task_id, device_id, lease_expires_at, attempt_count, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(namespace, task_id) DO UPDATE SET device_id=EXCLUDED.device_id, lease_expires_at=EXCLUDED.lease_expires_at, attempt_count=EXCLUDED.attempt_count, updated_at=EXCLUDED.updated_at
                    """,
                    (ns, v["task_id"], v["claimed_by_device_id"], v["lease_expires_at"], v["attempt_count"], v["updated_at"]),
                )
        for item in self.approvals.values():
            v = asdict(item)
            cur.execute(
                """
                INSERT INTO omnidesk_appsync_approvals(namespace, approval_id, task_id, risk, action, reason, requested_by, status, decided_by, decision_reason, decision_idempotency_key, created_at, decided_at, expires_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace, approval_id) DO UPDATE SET status=EXCLUDED.status, decided_by=EXCLUDED.decided_by, decision_reason=EXCLUDED.decision_reason, decision_idempotency_key=EXCLUDED.decision_idempotency_key, decided_at=EXCLUDED.decided_at
                """,
                (ns, v["approval_id"], v["task_id"], v["risk"], v["action"], v["reason"], v["requested_by"], v["status"], v.get("decided_by"), v.get("decision_reason"), v.get("decision_idempotency_key"), v["created_at"], v.get("decided_at"), v.get("expires_at")),
            )
        for item in self.notifications.values():
            v = asdict(item)
            cur.execute(
                """
                INSERT INTO omnidesk_appsync_notifications(namespace, notification_id, audience, title, body, event_type, actor, related_id, read, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace, notification_id) DO UPDATE SET read=EXCLUDED.read
                """,
                (ns, v["notification_id"], v["audience"], v["title"], v["body"], v["event_type"], v["actor"], v.get("related_id"), v["read"], v["created_at"]),
            )
        for item in self.runtimes.values():
            v = asdict(item)
            cur.execute(
                """
                INSERT INTO omnidesk_appsync_runtime_status(namespace, device_id, status, version, hostname, active_task_id, capabilities, last_heartbeat_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace, device_id) DO UPDATE SET status=EXCLUDED.status, version=EXCLUDED.version, hostname=EXCLUDED.hostname, active_task_id=EXCLUDED.active_task_id, capabilities=EXCLUDED.capabilities, last_heartbeat_at=EXCLUDED.last_heartbeat_at
                """,
                (ns, v["device_id"], v["status"], v.get("version"), v.get("hostname"), v.get("active_task_id"), Jsonb(v.get("capabilities") or []), v["last_heartbeat_at"]),
            )
        for item in self.device_enrollments.values():
            v = asdict(item)
            cur.execute(
                """
                INSERT INTO omnidesk_appsync_device_enrollments(namespace, enrollment_id, device_type, requested_by, pairing_code_hash, status, device_id, public_key, created_at, expires_at, completed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace, enrollment_id) DO UPDATE SET status=EXCLUDED.status, device_id=EXCLUDED.device_id, public_key=EXCLUDED.public_key, completed_at=EXCLUDED.completed_at
                """,
                (ns, v["enrollment_id"], v["device_type"], v["requested_by"], v["pairing_code_hash"], v["status"], v.get("device_id"), v.get("public_key"), v["created_at"], v["expires_at"], v.get("completed_at")),
            )
        for scoped, item in self.idempotency.items():
            cur.execute(
                """
                INSERT INTO omnidesk_appsync_idempotency_keys(namespace, organization_id, actor, endpoint, key, payload_hash, response, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace, organization_id, actor, endpoint, key) DO UPDATE SET response=EXCLUDED.response
                """,
                (ns, item.get("organization_id", self.organization.organization_id), item.get("actor", "unknown"), item.get("endpoint", "unknown"), item.get("key", scoped), item.get("payload_hash", ""), Jsonb(item.get("response", {})), item.get("created_at", 0.0)),
            )
        for item in self.push_outbox.values():
            v = asdict(item)
            cur.execute(
                """
                INSERT INTO omnidesk_appsync_push_outbox(namespace, push_id, device_id, platform, push_token, audience, title, body, related_id, status, attempt_count, last_error, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace, push_id) DO UPDATE SET status=EXCLUDED.status, attempt_count=EXCLUDED.attempt_count, last_error=EXCLUDED.last_error, updated_at=EXCLUDED.updated_at
                """,
                (ns, v["push_id"], v["device_id"], v["platform"], self.devices.get(v["device_id"], DeviceRecord(device_id=v["device_id"], device_type="mobile", name="unknown", platform=v["platform"], actor="system")).push_token, v["audience"], v["title"], v["body"], v.get("related_id"), v["status"], v["attempt_count"], v.get("last_error"), v["created_at"], v["updated_at"]),
            )
        for item in self.device_challenges.values():
            v = asdict(item)
            cur.execute(
                """
                INSERT INTO omnidesk_appsync_device_challenges(namespace, challenge_id, enrollment_id, device_id, nonce_hash, status, created_at, expires_at, verified_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace, challenge_id) DO UPDATE SET status=EXCLUDED.status, verified_at=EXCLUDED.verified_at
                """,
                (ns, v["challenge_id"], v["enrollment_id"], v["device_id"], v["nonce_hash"], v["status"], v["created_at"], v["expires_at"], v.get("verified_at")),
            )
        for event in self.events[-1000:]:
            cur.execute(
                """
                INSERT INTO omnidesk_appsync_sync_events(namespace, seq, event_type, actor, payload, created_at)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace, seq) DO NOTHING
                """,
                (ns, int(event.get("seq", 0)), str(event.get("event_type", "unknown")), str(event.get("actor", "unknown")), Jsonb(event.get("payload", {})), float(event.get("ts", 0.0))),
            )


    def _row_to_task(self, row: Any) -> dict[str, Any]:
        if row is None:
            raise KeyError("task not found")
        if not isinstance(row, dict):
            keys = ["namespace", "task_id", "conversation_id", "title", "actor", "organization_id", "status", "assigned_runtime_device_id", "requires_desktop_runtime", "approval_id", "result_summary", "idempotency_key", "claimed_by_device_id", "lease_expires_at", "attempt_count", "created_at", "updated_at"]
            row = {key: row[idx] for idx, key in enumerate(keys) if idx < len(row)}
        return {k: v for k, v in row.items() if k != "namespace"}

    def claim_next_task(self, *, actor: str, device_id: str, lease_seconds: int = 60, capabilities: list[str] | None = None) -> dict[str, Any] | None:
        import time
        lease_seconds = max(15, min(int(lease_seconds or 60), 600))
        now = time.time()
        params = {"namespace": self.namespace, "device_id": device_id, "now": now, "lease_expires_at": now + lease_seconds}
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(CLAIM_NEXT_TASK_SQL, params)
                row = cur.fetchone()
            conn.commit()
        if not row:
            return None
        task = self._row_to_task(row)
        self.tasks[task["task_id"]] = TaskRecord(**task)
        self._event("task.claimed", actor, {"task_id": task["task_id"], "device_id": device_id, "lease_expires_at": task.get("lease_expires_at"), "capabilities": list(capabilities or [])})
        self._persist()
        return task

    def update_task_status(self, *, task_id: str, actor: str, status: str, result_summary: str | None = None, assigned_runtime_device_id: str | None = None, idempotency_key: str | None = None, idempotency_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        import time
        now = time.time()
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE omnidesk_appsync_tasks
                    SET status=%(status)s, result_summary=COALESCE(%(result_summary)s, result_summary), assigned_runtime_device_id=COALESCE(%(assigned_runtime_device_id)s, assigned_runtime_device_id), lease_expires_at=CASE WHEN %(status)s IN ('completed','failed','cancelled') THEN NULL ELSE lease_expires_at END, updated_at=%(now)s
                    WHERE namespace=%(namespace)s AND task_id=%(task_id)s
                    RETURNING namespace, task_id, conversation_id, title, actor, organization_id, status, assigned_runtime_device_id, requires_desktop_runtime, approval_id, result_summary, idempotency_key, claimed_by_device_id, lease_expires_at, attempt_count, created_at, updated_at
                    """,
                    {"namespace": self.namespace, "task_id": task_id, "status": status, "result_summary": result_summary, "assigned_runtime_device_id": assigned_runtime_device_id, "now": now},
                )
                row = cur.fetchone()
            conn.commit()
        if not row:
            raise KeyError("task not found in postgres appsync source of truth")
        task = self._row_to_task(row)
        self.tasks[task["task_id"]] = TaskRecord(**task)
        self._event("task.status", actor, {"task_id": task_id, "status": status})
        self._persist()
        return task

    def renew_task_lease(self, *, actor: str, task_id: str, device_id: str, lease_seconds: int = 60) -> dict[str, Any]:
        import time
        now = time.time()
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(LEASE_RENEWAL_SQL, {"namespace": self.namespace, "task_id": task_id, "device_id": device_id, "now": now, "lease_expires_at": now + max(15, min(int(lease_seconds or 60), 600))})
                row = cur.fetchone()
            conn.commit()
        if not row:
            raise KeyError("lease not found")
        task = self._row_to_task(row)
        self.tasks[task["task_id"]] = TaskRecord(**task)
        self._event("task.lease_renewed", actor, {"task_id": task_id, "device_id": device_id, "lease_expires_at": task.get("lease_expires_at")})
        self._persist()
        return task


CLAIM_NEXT_TASK_SQL = """
WITH candidate AS (
    SELECT t.task_id
    FROM omnidesk_appsync_tasks t
    LEFT JOIN omnidesk_appsync_approvals a ON a.namespace = t.namespace AND a.approval_id = t.approval_id
    WHERE t.namespace = %(namespace)s
      AND t.requires_desktop_runtime = true
      AND t.status IN ('queued', 'running')
      AND (t.status = 'queued' OR t.lease_expires_at IS NULL OR t.lease_expires_at <= %(now)s)
      AND (t.approval_id IS NULL OR a.status = 'approved')
    ORDER BY t.created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE omnidesk_appsync_tasks t
SET status = 'running', assigned_runtime_device_id = %(device_id)s, claimed_by_device_id = %(device_id)s, lease_expires_at = %(lease_expires_at)s, attempt_count = t.attempt_count + 1, updated_at = %(now)s
FROM candidate
WHERE t.namespace = %(namespace)s AND t.task_id = candidate.task_id
RETURNING t.namespace, t.task_id, t.conversation_id, t.title, t.actor, t.organization_id, t.status, t.assigned_runtime_device_id, t.requires_desktop_runtime, t.approval_id, t.result_summary, t.idempotency_key, t.claimed_by_device_id, t.lease_expires_at, t.attempt_count, t.created_at, t.updated_at
"""

IDEMPOTENCY_INSERT_SQL = """
INSERT INTO omnidesk_appsync_idempotency_keys(namespace, organization_id, actor, endpoint, key, payload_hash, response, created_at)
VALUES (%(namespace)s, %(organization_id)s, %(actor)s, %(endpoint)s, %(key)s, %(payload_hash)s, %(response)s, %(created_at)s)
ON CONFLICT(namespace, organization_id, actor, endpoint, key) DO NOTHING
"""

LEASE_RENEWAL_SQL = """
UPDATE omnidesk_appsync_tasks
SET lease_expires_at = %(lease_expires_at)s, updated_at = %(now)s
WHERE namespace = %(namespace)s AND task_id = %(task_id)s AND claimed_by_device_id = %(device_id)s AND status = 'running'
RETURNING namespace, task_id, conversation_id, title, actor, organization_id, status, assigned_runtime_device_id, requires_desktop_runtime, approval_id, result_summary, idempotency_key, claimed_by_device_id, lease_expires_at, attempt_count, created_at, updated_at
"""
