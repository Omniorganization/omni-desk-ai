from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from omnidesk_agent.config import AppConfig, ChromeConfig, UIBridgeConfig
from omnidesk_agent.daemon import OmniDeskRuntime
from omnidesk_agent.storage.sqlite import close_all_open_connections, connect_sqlite


def _runtime_config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.workspace.root = tmp_path
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.token_file = tmp_path / "google" / "token.json"
    cfg.learning.growth_plan_file = tmp_path / "growth_plan.json"
    cfg.plugins.enabled = False
    cfg.ensure_dirs()
    return cfg


def test_close_all_open_connections_skips_active_context_connection(tmp_path):
    with connect_sqlite(tmp_path / "active.sqlite3") as con:
        con.execute("CREATE TABLE demo(id INTEGER PRIMARY KEY)")
        close_all_open_connections()
        con.execute("INSERT INTO demo(id) VALUES(1)")

    with pytest.raises(Exception):
        con.execute("SELECT 1")


def test_runtime_close_does_not_close_unrelated_active_connection(tmp_path):
    cfg = _runtime_config(tmp_path / "runtime")
    rt = OmniDeskRuntime(cfg)
    with connect_sqlite(tmp_path / "external.sqlite3") as con:
        con.execute("CREATE TABLE demo(id INTEGER PRIMARY KEY)")
        rt.close()
        con.execute("INSERT INTO demo(id) VALUES(1)")


def test_cli_runtime_context_runs_production_validator(monkeypatch, tmp_path):
    from omnidesk_agent import cli

    cfg = _runtime_config(tmp_path)
    seen = {"called": False}

    def fake_assert(candidate):
        assert candidate is cfg
        seen["called"] = True

    class FakeRuntime:
        def __init__(self, candidate):
            assert candidate is cfg
            self.closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(cli, "assert_production_config_safe", fake_assert)
    monkeypatch.setattr(cli, "OmniDeskRuntime", FakeRuntime)

    with cli.runtime_context(cfg) as rt:
        assert isinstance(rt, FakeRuntime)

    assert seen["called"] is True
    assert rt.closed is True


def test_cli_runtime_context_closes_escaped_sqlite_connections(monkeypatch, tmp_path):
    from omnidesk_agent import cli

    cfg = _runtime_config(tmp_path)
    leaked = {}

    def fake_assert(candidate):
        assert candidate is cfg
        con = connect_sqlite(tmp_path / "validator_leak.sqlite3")
        con.execute("CREATE TABLE demo(id INTEGER PRIMARY KEY)")
        leaked["con"] = con

    class FakeRuntime:
        def __init__(self, candidate):
            assert candidate is cfg

    monkeypatch.setattr(cli, "assert_production_config_safe", fake_assert)
    monkeypatch.setattr(cli, "OmniDeskRuntime", FakeRuntime)

    with cli.runtime_context(cfg) as rt:
        assert isinstance(rt, FakeRuntime)

    with pytest.raises(sqlite3.ProgrammingError):
        leaked["con"].execute("SELECT 1")


def test_cli_runtime_context_closes_sqlite_when_validator_fails(monkeypatch, tmp_path):
    from omnidesk_agent import cli

    cfg = _runtime_config(tmp_path)
    leaked = {}

    def fake_assert(candidate):
        assert candidate is cfg
        con = connect_sqlite(tmp_path / "failed_validator_leak.sqlite3")
        con.execute("CREATE TABLE demo(id INTEGER PRIMARY KEY)")
        leaked["con"] = con
        raise RuntimeError("validator failed")

    class FakeRuntime:
        def __init__(self, candidate):
            raise AssertionError("runtime should not start after validator failure")

    monkeypatch.setattr(cli, "assert_production_config_safe", fake_assert)
    monkeypatch.setattr(cli, "OmniDeskRuntime", FakeRuntime)

    with pytest.raises(RuntimeError, match="validator failed"):
        with cli.runtime_context(cfg):
            pass

    with pytest.raises(sqlite3.ProgrammingError):
        leaked["con"].execute("SELECT 1")


def test_high_risk_capabilities_are_disabled_by_default(tmp_path):
    assert ChromeConfig().enabled is False
    assert UIBridgeConfig().enabled is False

    cfg = _runtime_config(tmp_path)
    rt = OmniDeskRuntime(cfg)
    try:
        names = set(rt.tools.names())
        assert {"files", "test", "vision"} <= names
        assert {"shell", "computer", "git", "pull_request", "browser", "ui_bridge", "gmail", "channels"}.isdisjoint(names)
    finally:
        rt.close()


def test_capability_registry_exposes_only_enabled_high_risk_tools(tmp_path):
    cfg = _runtime_config(tmp_path)
    cfg.capabilities.shell.enabled = True
    cfg.capabilities.git.enabled = True
    cfg.capabilities.computer.enabled = True
    cfg.capabilities.pull_request.enabled = True
    cfg.capabilities.browser.enabled = True
    cfg.channels.chrome.enabled = True
    cfg.channels.chrome.allowed_origins = ["https://example.test"]

    rt = OmniDeskRuntime(cfg)
    try:
        assert {"shell", "git", "computer", "pull_request", "browser"} <= set(rt.tools.names())
    finally:
        rt.close()


def test_check_slo_help_and_flags(tmp_path, capsys):
    from scripts import check_slo

    with pytest.raises(SystemExit) as exc:
        check_slo.main(["--help"])
    assert exc.value.code == 0
    assert "--metrics-file" in capsys.readouterr().out

    metrics = {
        "webhook_enqueue_success_rate": 1.0,
        "job_dead_letter_rate": 0.0,
        "approval_resume_success_rate": 1.0,
        "planner_fallback_rate": 0.0,
        "tool_error_rate": 0.0,
        "outbound_duplicate_rate": 0.0,
        "plugin_timeout_rate": 0.0,
        "daily_model_cost_usd": 1.0,
        "cost_per_successful_task": 1.0,
    }
    metrics_path = tmp_path / "metrics.json"
    metrics_path.write_text(json.dumps(metrics), encoding="utf-8")

    assert check_slo.main(["--metrics-file", str(metrics_path), "--json", "--fail-on-error-budget"]) == 0


def test_release_hygiene_rejects_macos_metadata(tmp_path, capsys):
    from scripts import check_release_hygiene

    (tmp_path / "__MACOSX").mkdir()
    (tmp_path / ".DS_Store").write_bytes(b"metadata")

    assert check_release_hygiene.main(str(tmp_path)) == 1
    err = capsys.readouterr().err
    assert "__MACOSX" in err
    assert ".DS_Store" in err


def test_release_hygiene_rejects_git_metadata(tmp_path, capsys):
    from scripts import check_release_hygiene

    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[remote]\n", encoding="utf-8")

    assert check_release_hygiene.main(str(tmp_path)) == 1
    assert check_release_hygiene.main(str(tmp_path), "--allow-vcs") == 0
    err = capsys.readouterr().err
    assert ".git" in err


def test_github_workflows_pin_actions_and_permissions():
    workflow_dir = Path(".github/workflows")
    for workflow in workflow_dir.glob("*.yml"):
        text = workflow.read_text(encoding="utf-8")
        assert "permissions:" in text, workflow
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("- uses: "):
                continue
            ref = stripped.split("@", 1)[1]
            assert len(ref) == 40 and all(ch in "0123456789abcdef" for ch in ref), f"{workflow}: {stripped}"


def test_current_tree_release_hygiene_is_clean():
    import shutil
    from scripts import check_release_hygiene

    for cache_dir in Path(".").rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)
    for cache_dir in Path(".").rglob(".pytest_cache"):
        shutil.rmtree(cache_dir, ignore_errors=True)
    for cache_dir in Path(".").rglob(".ruff_cache"):
        shutil.rmtree(cache_dir, ignore_errors=True)
    for cache_dir in Path(".").rglob(".serena"):
        shutil.rmtree(cache_dir, ignore_errors=True)
    for generated_dir_name in ["node_modules", ".next", ".npm-cache", ".mypy_cache", ".dart_tool", "target", "build", "dist", "coverage", "htmlcov"]:
        for generated_dir in Path(".").rglob(generated_dir_name):
            shutil.rmtree(generated_dir, ignore_errors=True)
    for artifact in Path(".").rglob(".DS_Store"):
        artifact.unlink(missing_ok=True)
    for artifact in Path(".").rglob("*.tsbuildinfo"):
        artifact.unlink(missing_ok=True)
    for artifact in [Path(".coverage"), Path("coverage.json"), Path("coverage.xml")]:
        artifact.unlink(missing_ok=True)

    assert check_release_hygiene.main(".", "--allow-vcs") == 0


def test_upgrade_security_checker_rejects_paths_outside_repo(tmp_path):
    from omnidesk_agent.self_upgrade.security_checker import UpgradeSecurityChecker

    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside_secret.py"
    outside.write_text("eval('bad')", encoding="utf-8")

    result = UpgradeSecurityChecker().check_files(repo, ["../outside_secret.py"])

    assert result["ok"] is False
    assert result["issues"][0]["issue"] == "path outside repository"
