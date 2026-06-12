from __future__ import annotations

from pathlib import Path


def test_docker_compose_mounts_production_config_and_binds_all_interfaces():
    compose = Path("deploy/docker/docker-compose.yml").read_text(encoding="utf-8")
    assert "./config.production.yaml:/data/config.yaml:ro" in compose
    assert '"--host", "0.0.0.0"' in compose
    assert "OMNIDESK_MEMORY_ENCRYPTION_KEY" in compose


def test_container_production_example_config_writes_only_under_data():
    cfg = Path("deploy/docker/config.production.example.yaml").read_text(encoding="utf-8")
    assert "root: /data/workspace" in cfg
    assert "memory_db: /data/memory.sqlite3" in cfg
    assert "audit_log: /data/audit.log" in cfg
    assert "host: 0.0.0.0" in cfg
    assert "encrypt_at_rest: true" in cfg
