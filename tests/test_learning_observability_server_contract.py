from __future__ import annotations

import pytest


def test_server_defines_admin_learning_routes(tmp_path):
    pytest.importorskip("fastapi")
    from omnidesk_agent.config import AppConfig
    from omnidesk_agent.server import create_app

    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "workspace"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.permissions.audit_log = tmp_path / "audit.log"
    app = create_app(cfg)
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/admin/learning/report" in paths
    assert "/admin/learning/dashboard" in paths
