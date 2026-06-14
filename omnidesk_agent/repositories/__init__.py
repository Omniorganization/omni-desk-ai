from __future__ import annotations

from .base import RepositoryCapabilities, RepositoryFactory, TransactionalOutboxRepository
from .sqlite import SQLiteRepositoryFactory, SQLiteTransactionalOutboxRepository
from .postgres import PostgresRepositoryFactory, PostgresTransactionalOutboxRepository
from omnidesk_agent.repositories.runtime import build_repository_factory, storage_plan

__all__ = [
    "RepositoryCapabilities",
    "RepositoryFactory",
    "TransactionalOutboxRepository",
    "SQLiteRepositoryFactory",
    "SQLiteTransactionalOutboxRepository",
    "PostgresRepositoryFactory",
    "PostgresTransactionalOutboxRepository",
    "build_repository_factory",
    "storage_plan",
]
