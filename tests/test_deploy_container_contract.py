from __future__ import annotations

import subprocess
import sys
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


def test_web_admin_container_hardening_contract_passes_current_tree():
    result = subprocess.run(
        [sys.executable, "scripts/check_web_admin_container_hardening.py", "."],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_web_admin_dockerfile_is_pinned_non_root_and_healthchecked():
    dockerfile = Path("apps/web-admin-next/Dockerfile").read_text(encoding="utf-8")
    security_doc = Path("apps/web-admin-next/SECURITY_RELEASE.md").read_text(encoding="utf-8")

    assert "NODE_BASE_IMAGE=node:22-bookworm-slim@sha256:" in dockerfile
    assert "FROM node:" not in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "HEALTHCHECK --interval=30s --timeout=5s --retries=3" in dockerfile
    assert 'VOLUME ["/tmp"]' in dockerfile
    assert "--read-only" in security_doc
    assert "--security-opt no-new-privileges:true" in security_doc
