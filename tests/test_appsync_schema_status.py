"""Readiness can ask the PostgreSQL AppSync store whether its schema is current.

Security's readiness check (#245) calls ``rt.app_sync.schema_status()`` when it
exists and fails readiness when the namespace is behind. Before, readiness
reported the schema as current without checking.
"""

from __future__ import annotations

import os
import uuid

import pytest

from omnidesk_agent.appsync.postgres_migrations import LATEST_SCHEMA_VERSION, MIGRATIONS, apply_appsync_migrations, appsync_schema_status


def test_schema_status_reports_the_namespace_migrations() -> None:
    dsn = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("OMNIDESK_TEST_POSTGRES_DSN is not configured")
    # Only after the skip: the release, staging and promotion gates install only
    # requirements.dev.lock, which has no psycopg.
    psycopg = pytest.importorskip("psycopg")
    from omnidesk_agent.appsync.migrated_postgres_store import MigratedMultiInstancePostgresAppSyncStore

    namespace = f"schema_status_{uuid.uuid4().hex}"
    apply_appsync_migrations(dsn, namespace=namespace)
    store = MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=namespace, pool_size=1)
    try:
        assert store.schema_status() == {"current": LATEST_SCHEMA_VERSION, "required": LATEST_SCHEMA_VERSION, "ready": True}
    finally:
        store.close()
    # What schema_status wraps reports a namespace one migration behind as not
    # ready (the store itself refuses to start on such a namespace).
    behind = f"schema_behind_{uuid.uuid4().hex}"
    apply_appsync_migrations(dsn, namespace=behind, migrations=MIGRATIONS[:-1])
    with psycopg.connect(dsn) as conn:
        assert appsync_schema_status(conn, namespace=behind) == {"current": LATEST_SCHEMA_VERSION - 1, "required": LATEST_SCHEMA_VERSION, "ready": False}
