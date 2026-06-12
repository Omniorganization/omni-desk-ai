from __future__ import annotations

from pathlib import Path

from omnidesk_agent.config import AppConfig
from omnidesk_agent.daemon import OmniDeskRuntime


def test_runtime_wires_learning_loop_into_orchestrator(tmp_path: Path):
    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "workspace"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.token_file = tmp_path / "google" / "token.json"
    cfg.learning.growth_plan_file = tmp_path / "growth_plan.json"
    cfg.llm.provider = "rule"
    cfg.channels.chrome.enabled = False
    cfg.plugins.enabled = False
    cfg.memory_privacy.encrypt_at_rest = False
    cfg.ensure_dirs()

    rt = OmniDeskRuntime(cfg)
    try:
        assert rt.learning_loop is not None
        assert rt.orchestrator.learning_loop is rt.learning_loop
    finally:
        rt.close()
