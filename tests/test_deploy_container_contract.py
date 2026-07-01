from __future__ import annotations

from pathlib import Path


def test_docker_compose_mounts_production_config_and_binds_all_interfaces():
    compose = Path("deploy/docker/docker-compose.yml").read_text(encoding="utf-8")
    assert "./config.production.yaml:/data/config.yaml:ro" in compose
    assert '"--host", "0.0.0.0"' in compose
    assert "OMNIDESK_MEMORY_ENCRYPTION_KEY" in compose
    assert "OMNIDESK_REQUIRE_PRODUCTION_GUARDS" in compose
    assert "OMNIDESK_APPSYNC_SECRET_PEPPER" in compose


def test_runtime_dockerfile_defaults_to_production_guards():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "OMNIDESK_ENV=production" in dockerfile
    assert "OMNIDESK_REQUIRE_PRODUCTION_GUARDS=1" in dockerfile
    assert "OMNIDESK_RUNNING_IN_CONTAINER=1" in dockerfile


def test_container_production_example_config_writes_only_under_data():
    cfg = Path("deploy/docker/config.production.example.yaml").read_text(encoding="utf-8")
    assert "root: /data/workspace" in cfg
    assert "memory_db: /data/memory.sqlite3" in cfg
    assert "audit_log: /data/audit.log" in cfg
    assert "host: 0.0.0.0" in cfg
    assert "encrypt_at_rest: true" in cfg
    assert "api_resource_guard:" in cfg
    assert "backend: postgres" in cfg
    assert "postgres_dsn_env: OMNIDESK_POSTGRES_DSN" in cfg
    assert "secret_pepper_env: OMNIDESK_APPSYNC_SECRET_PEPPER" in cfg
    assert "per_task_max_llm_calls: 16" in cfg
    assert "daily_usd_limit: 500.0" in cfg
