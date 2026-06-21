from __future__ import annotations

from omnidesk_agent.config import ApiResourceGuardConfig, AppConfig
from omnidesk_agent.security.resource_guard import SQLiteRateLimiter
from omnidesk_agent.validation.production import validate_production_config


def test_sqlite_rate_limiter_persists_window_counts(tmp_path):
    db_path = tmp_path / "resource_guard.sqlite3"
    first = SQLiteRateLimiter(db_path)
    second = SQLiteRateLimiter(db_path)

    assert first.allow("ip:alice", limit=1, window_seconds=60)
    assert not second.allow("ip:alice", limit=1, window_seconds=60)
    assert second.size() == 1


def test_api_resource_guard_config_exposes_shared_backends(tmp_path):
    cfg = ApiResourceGuardConfig(backend="sqlite", sqlite_path=tmp_path / "guard.sqlite3")

    assert cfg.backend == "sqlite"
    assert cfg.sqlite_path.name == "guard.sqlite3"
    assert cfg.postgres_dsn_env == "OMNIDESK_POSTGRES_DSN"


def test_multi_instance_production_rejects_memory_resource_guard():
    cfg = AppConfig()
    cfg.plugins.enabled = False
    cfg.memory_privacy.encrypt_at_rest = True
    cfg.storage.backend = "postgres"
    cfg.storage.require_multi_instance_safe = True
    cfg.app_sync.backend = "postgres"
    cfg.api_resource_guard.backend = "memory"

    result = validate_production_config(
        cfg,
        {
            "OMNIDESK_ENV": "production",
            "OMNIDESK_ADMIN_TOKEN": "x" * 40,
            "OMNIDESK_GATEWAY_SECRET": "x" * 40,
            "OMNIDESK_MEMORY_ENCRYPTION_KEY": "x" * 40,
            "OMNIDESK_POSTGRES_DSN": "postgresql://user:pass@db/omnidesk",
            "OMNIDESK_APPSYNC_POSTGRES_DSN": "postgresql://user:pass@db/omnidesk",
        },
    )

    assert "api_resource_guard.backend must be postgres when storage.require_multi_instance_safe=true" in result["issues"]
