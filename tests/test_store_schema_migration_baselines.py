from __future__ import annotations

from omnidesk_agent.storage.sqlite import connect_sqlite

from omnidesk_agent.core.job_queue import JobQueue
from omnidesk_agent.core.run_store import RunStore
from omnidesk_agent.core.token_budget import TokenBudgetManager
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.oauth.state_store import OAuthStateStore
from omnidesk_agent.security.approval_store import ApprovalStore


def _migration_names(db_path):
    with connect_sqlite(db_path) as con:
        return {row[0] for row in con.execute("SELECT name FROM schema_migrations")}


def test_core_stores_share_schema_migration_table(tmp_path):
    stores = [
        (RunStore, "runs.sqlite3", "run_store_schema_baseline"),
        (JobQueue, "jobs.sqlite3", "job_queue_schema_baseline"),
        (ApprovalStore, "approvals.sqlite3", "approval_store_schema_baseline"),
        (OAuthStateStore, "oauth.sqlite3", "oauth_state_schema_baseline"),
        (TokenBudgetManager, "tokens.sqlite3", "token_budget_schema_baseline"),
    ]
    for cls, name, migration in stores:
        cls(tmp_path / name)
        assert migration in _migration_names(tmp_path / name)

    memory = ExperienceStore(tmp_path / "memory.sqlite3")
    try:
        assert "experience_store_schema_baseline" in _migration_names(tmp_path / "memory.sqlite3")
    finally:
        memory.close()
