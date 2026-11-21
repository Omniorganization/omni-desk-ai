"""Frozen baseline DDL of AppSync PostgreSQL migration 1.

The text is checksummed by ``postgres_migrations`` once applied, so it must never
change; schema changes go into new migrations. The store that uses these tables
is ``migrated_postgres_store.MigratedMultiInstancePostgresAppSyncStore``.
"""

from __future__ import annotations


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
    organization_id TEXT NOT NULL DEFAULT 'org_default',
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
    organization_id TEXT NOT NULL DEFAULT 'org_default',
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
ALTER TABLE omnidesk_appsync_conversations ADD COLUMN IF NOT EXISTS organization_id TEXT NOT NULL DEFAULT 'org_default';
ALTER TABLE omnidesk_appsync_messages ADD COLUMN IF NOT EXISTS organization_id TEXT NOT NULL DEFAULT 'org_default';
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
    organization_id TEXT NOT NULL DEFAULT 'org_default',
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
    organization_id TEXT NOT NULL DEFAULT 'org_default',
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
    organization_id TEXT NOT NULL DEFAULT 'org_default',
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
    organization_id TEXT NOT NULL DEFAULT 'org_default',
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
    organization_id TEXT NOT NULL DEFAULT 'org_default',
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
    namespace TEXT NOT NULL, organization_id TEXT NOT NULL DEFAULT 'org_default', push_id TEXT NOT NULL, device_id TEXT NOT NULL, platform TEXT NOT NULL, push_token TEXT, audience TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, related_id TEXT, status TEXT NOT NULL DEFAULT 'pending', attempt_count INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL, PRIMARY KEY(namespace, push_id)
);
CREATE INDEX IF NOT EXISTS omnidesk_appsync_push_pending_idx ON omnidesk_appsync_push_outbox(namespace, status, created_at);
CREATE TABLE IF NOT EXISTS omnidesk_appsync_device_challenges (
    namespace TEXT NOT NULL, organization_id TEXT NOT NULL DEFAULT 'org_default', challenge_id TEXT NOT NULL, enrollment_id TEXT NOT NULL, device_id TEXT NOT NULL, nonce_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL, verified_at DOUBLE PRECISION, PRIMARY KEY(namespace, challenge_id)
);
CREATE TABLE IF NOT EXISTS omnidesk_appsync_local_outbox (
    namespace TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    operation_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    actor TEXT NOT NULL,
    device_id TEXT,
    idempotency_key TEXT,
    payload_hash TEXT NOT NULL,
    conflict_policy TEXT NOT NULL,
    status TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at DOUBLE PRECISION,
    remote_seq BIGINT,
    last_error TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(namespace, operation_id)
);
CREATE INDEX IF NOT EXISTS omnidesk_appsync_local_outbox_pending_idx ON omnidesk_appsync_local_outbox(namespace, organization_id, status, created_at);
CREATE TABLE IF NOT EXISTS omnidesk_appsync_local_inbox (
    namespace TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    remote_event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    actor TEXT NOT NULL,
    source_device_id TEXT,
    idempotency_key TEXT,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    received_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(namespace, remote_event_id)
);
CREATE TABLE IF NOT EXISTS omnidesk_appsync_sync_cursors (
    namespace TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    cursor_id TEXT NOT NULL,
    remote TEXT NOT NULL,
    actor TEXT NOT NULL,
    device_id TEXT,
    since_seq BIGINT NOT NULL DEFAULT 0,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(namespace, cursor_id)
);
CREATE TABLE IF NOT EXISTS omnidesk_appsync_sync_conflicts (
    namespace TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    conflict_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    remote_event_id TEXT,
    conflict_type TEXT NOT NULL,
    strategy TEXT NOT NULL,
    status TEXT NOT NULL,
    actor TEXT NOT NULL,
    local_payload JSONB NOT NULL,
    remote_payload JSONB NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    resolved_at DOUBLE PRECISION,
    PRIMARY KEY(namespace, conflict_id)
);
CREATE TABLE IF NOT EXISTS omnidesk_appsync_operation_log (
    namespace TEXT NOT NULL,
    organization_id TEXT NOT NULL DEFAULT 'org_default',
    seq BIGINT NOT NULL,
    event TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(namespace, seq, event)
);
ALTER TABLE omnidesk_appsync_approvals ADD COLUMN IF NOT EXISTS organization_id TEXT NOT NULL DEFAULT 'org_default';
ALTER TABLE omnidesk_appsync_notifications ADD COLUMN IF NOT EXISTS organization_id TEXT NOT NULL DEFAULT 'org_default';
ALTER TABLE omnidesk_appsync_runtime_status ADD COLUMN IF NOT EXISTS organization_id TEXT NOT NULL DEFAULT 'org_default';
ALTER TABLE omnidesk_appsync_device_enrollments ADD COLUMN IF NOT EXISTS organization_id TEXT NOT NULL DEFAULT 'org_default';
ALTER TABLE omnidesk_appsync_sync_events ADD COLUMN IF NOT EXISTS organization_id TEXT NOT NULL DEFAULT 'org_default';
ALTER TABLE omnidesk_appsync_push_outbox ADD COLUMN IF NOT EXISTS organization_id TEXT NOT NULL DEFAULT 'org_default';
ALTER TABLE omnidesk_appsync_device_challenges ADD COLUMN IF NOT EXISTS organization_id TEXT NOT NULL DEFAULT 'org_default';
ALTER TABLE omnidesk_appsync_devices ADD COLUMN IF NOT EXISTS approval_public_key TEXT;
ALTER TABLE omnidesk_appsync_devices ADD COLUMN IF NOT EXISTS approval_key_registered_at DOUBLE PRECISION;
"""
