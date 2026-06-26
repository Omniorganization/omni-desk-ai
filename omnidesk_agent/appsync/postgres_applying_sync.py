from __future__ import annotations

from omnidesk_agent.appsync.offline_sync_apply import ApplyingAppSyncMixin
from omnidesk_agent.appsync.postgres_offline_sync import DurablePostgresAppSyncStore


class ApplyingDurablePostgresAppSyncStore(ApplyingAppSyncMixin, DurablePostgresAppSyncStore):
    """PostgreSQL AppSync store with durable offline sync and formal operation application."""
