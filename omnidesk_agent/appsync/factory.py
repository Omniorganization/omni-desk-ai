from __future__ import annotations

import os
from typing import Any

from omnidesk_agent.appsync.migrated_postgres_store import (
    MigratedMultiInstancePostgresAppSyncStore,
)
from omnidesk_agent.appsync.store import AppSyncStore
from omnidesk_agent.appsync.strict_json_store import StrictJsonAppSyncStore
from omnidesk_agent.validation.production import is_production_mode


def create_appsync_store(cfg: Any) -> AppSyncStore:
    app_sync_cfg = getattr(cfg, "app_sync", None)
    backend = (
        getattr(app_sync_cfg, "backend", "json")
        if app_sync_cfg is not None
        else "json"
    )
    offline = bool(getattr(getattr(cfg, "runtime", None), "offline_mode", False))

    if backend == "postgres":
        dsn_env = getattr(
            app_sync_cfg,
            "postgres_dsn_env",
            "OMNIDESK_APPSYNC_POSTGRES_DSN",
        )
        dsn = os.getenv(dsn_env, "")
        if not dsn:
            raise RuntimeError(
                f"Missing PostgreSQL AppSync DSN environment variable: {dsn_env}"
            )
        namespace = getattr(app_sync_cfg, "namespace", "default")
        return MigratedMultiInstancePostgresAppSyncStore(
            dsn=dsn,
            namespace=namespace,
            local_outbox_enabled=offline,
        )

    storage_cfg = getattr(cfg, "storage", None)
    if is_production_mode(cfg) or bool(
        getattr(storage_cfg, "require_multi_instance_safe", False)
    ):
        raise RuntimeError(
            "JSON AppSync storage is development-only; production and "
            "multi-instance deployments require app_sync.backend=postgres"
        )
    path = getattr(app_sync_cfg, "json_path", None) if app_sync_cfg else None
    target = path or (cfg.workspace.root / "app_sync_state.json")
    return StrictJsonAppSyncStore(
        target,
        local_outbox_enabled=offline,
    )
