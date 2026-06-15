from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from omnidesk_agent.repositories.base import RepositoryFactory
from omnidesk_agent.repositories.postgres import PostgresRepositoryFactory, PostgresUnavailable
from omnidesk_agent.repositories.sqlite import SQLiteRepositoryFactory

StorageBackend = Literal["sqlite", "postgres"]


@dataclass(frozen=True)
class RuntimeStoragePlan:
    """Runtime storage governance plan used at process startup.

    GA13 deliberately makes the storage backend explicit. SQLite remains the
    default local-first single-node backend; production multi-instance mode must
    opt into PostgreSQL and provide a DSN. This prevents accidental horizontal
    scaling on local SQLite files.
    """

    backend: StorageBackend
    multi_instance_safe: bool
    transactional_outbox: bool
    reason: str


def build_repository_factory(*, backend: str, workspace_root: Path, postgres_dsn_env: str = "OMNIDESK_POSTGRES_DSN") -> RepositoryFactory:
    normalized = backend.lower().strip()
    if normalized == "sqlite":
        return SQLiteRepositoryFactory(Path(workspace_root) / "transactional_outbox.sqlite3")
    if normalized == "postgres":
        dsn = os.getenv(postgres_dsn_env, "")
        if not dsn:
            raise PostgresUnavailable(f"{postgres_dsn_env} is required when storage.backend=postgres")
        factory = PostgresRepositoryFactory(dsn)
        # Initialize the multi-instance outbox schema early so deploy/readiness
        # probes fail before traffic is accepted.
        factory.transactional_outbox().init_schema()
        return factory
    raise ValueError(f"unsupported storage backend: {backend!r}")


def storage_plan(*, backend: str, require_multi_instance_safe: bool) -> RuntimeStoragePlan:
    normalized = backend.lower().strip()
    if normalized == "postgres":
        return RuntimeStoragePlan(
            backend="postgres",
            multi_instance_safe=True,
            transactional_outbox=True,
            reason="PostgreSQL backend supports row-level locking, transactional outbox, and multi-instance runtime claims.",
        )
    if normalized == "sqlite":
        if require_multi_instance_safe:
            raise RuntimeError("storage.require_multi_instance_safe=true requires storage.backend=postgres")
        return RuntimeStoragePlan(
            backend="sqlite",
            multi_instance_safe=False,
            transactional_outbox=True,
            reason="SQLite backend is local-first and single-node only; use postgres for HA/multi-instance production.",
        )
    raise ValueError(f"unsupported storage backend: {backend!r}")
