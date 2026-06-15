from __future__ import annotations

import os
from typing import Any

from omnidesk_agent.appsync.store import AppSyncStore


def create_appsync_store(cfg: Any) -> AppSyncStore:
    app_sync_cfg = getattr(cfg, "app_sync", None)
    backend = getattr(app_sync_cfg, "backend", "json") if app_sync_cfg is not None else "json"
    if backend == "postgres":
        from omnidesk_agent.appsync.postgres_store import PostgresAppSyncStore

        dsn_env = getattr(app_sync_cfg, "postgres_dsn_env", "OMNIDESK_APPSYNC_POSTGRES_DSN")
        dsn = os.getenv(dsn_env, "")
        if not dsn:
            raise RuntimeError(f"Missing PostgreSQL AppSync DSN environment variable: {dsn_env}")
        namespace = getattr(app_sync_cfg, "namespace", "default")
        return PostgresAppSyncStore(dsn=dsn, namespace=namespace)

    path = getattr(app_sync_cfg, "json_path", None) if app_sync_cfg is not None else None
    if path:
        return AppSyncStore(path)
    return AppSyncStore(cfg.workspace.root / "app_sync_state.json")
