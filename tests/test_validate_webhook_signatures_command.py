"""`omnidesk validate-webhook-signatures` checks the live verification path.

The 2026-11-21 audit (runtime probe) found the command printed a hard-coded
{"ok": true}. It must now fail when signatures are not enforced, when an
enabled channel's secrets are missing, or when the guard would accept an
unsigned or wrongly signed delivery.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from omnidesk_agent.config import AppConfig
from omnidesk_agent.daemon import OmniDeskRuntime
from omnidesk_agent.server_routes.webhook_guard import self_test_webhook_signatures

REPO_ROOT = Path(__file__).resolve().parents[1]
TELEGRAM_ENV = {"TELEGRAM_BOT_TOKEN": "123:" + "b" * 30, "TELEGRAM_WEBHOOK_SECRET": "s" * 40}


def _cfg(tmp_path: Path, *, telegram: bool, enforce: bool = True) -> AppConfig:
    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "ws"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.learning.growth_plan_file = tmp_path / "growth.json"
    cfg.channels.gmail.credentials_file = tmp_path / "g" / "c.json"
    cfg.channels.gmail.token_file = tmp_path / "g" / "t.json"
    cfg.plugins.enabled = False
    cfg.channels.telegram.enabled = telegram
    cfg.channels.telegram.allowed_user_ids = [1]
    cfg.gateway.require_webhook_signatures = enforce
    cfg.ensure_dirs()
    return cfg


def _run(cfg: AppConfig, env: dict[str, str]) -> dict:
    rt = OmniDeskRuntime(cfg)
    try:
        return self_test_webhook_signatures(cfg, rt, environ=env)
    finally:
        rt.close()


def test_a_configured_channel_refuses_unsigned_and_forged_deliveries(tmp_path, monkeypatch):
    for name, value in TELEGRAM_ENV.items():
        monkeypatch.setenv(name, value)

    result = _run(_cfg(tmp_path, telegram=True), TELEGRAM_ENV)

    assert result["ok"] is True
    assert result["channels"]["telegram"]["unsigned"].startswith("refused")
    assert result["channels"]["telegram"]["wrong_secret"].startswith("refused")


def test_unenforced_signatures_fail_the_check(tmp_path, monkeypatch):
    for name, value in TELEGRAM_ENV.items():
        monkeypatch.setenv(name, value)

    result = _run(_cfg(tmp_path, telegram=True, enforce=False), TELEGRAM_ENV)

    assert result["ok"] is False
    assert result["require_webhook_signatures"] is False
    assert result["channels"]["telegram"]["unsigned"] == "ACCEPTED"


def test_a_missing_secret_fails_the_check(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TELEGRAM_ENV["TELEGRAM_BOT_TOKEN"])
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)

    result = _run(_cfg(tmp_path, telegram=True), {"TELEGRAM_BOT_TOKEN": TELEGRAM_ENV["TELEGRAM_BOT_TOKEN"]})

    assert result["ok"] is False
    assert result["channels"]["telegram"]["missing_env"] == ["TELEGRAM_WEBHOOK_SECRET"]


def test_no_inbound_channel_is_reported_as_such(tmp_path):
    result = _run(_cfg(tmp_path, telegram=False), {})

    assert result == {"ok": True, "require_webhook_signatures": True, "channels": {}, "note": "no inbound channel is enabled"}


def test_cli_exit_code_follows_the_result(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "workspace": {"root": str(tmp_path / "ws"), "memory_db": str(tmp_path / "m.sqlite3"),
                      "skills_dirs": [str(tmp_path / "s")], "plugins_dirs": [str(tmp_path / "p")]},
        "permissions": {"audit_log": str(tmp_path / "audit.log")},
        "learning": {"growth_plan_file": str(tmp_path / "growth.json")},
        "plugins": {"enabled": False},
        "gateway": {"require_webhook_signatures": False},
        "channels": {"telegram": {"enabled": True, "allowed_user_ids": [1]},
                     "gmail": {"credentials_file": str(tmp_path / "c.json"), "token_file": str(tmp_path / "t.json")}},
    }))
    env = {**os.environ, **TELEGRAM_ENV, "PYTHONPATH": str(REPO_ROOT), "HOME": str(tmp_path)}

    # Same entry point as the `omnidesk` console script (pyproject: omnidesk_agent.cli:main).
    launcher = "import sys; from omnidesk_agent.cli import main; sys.argv = ['omnidesk', *sys.argv[1:]]; main()"
    failed = subprocess.run([sys.executable, "-c", launcher, "--config", str(config), "validate-webhook-signatures"],
                            capture_output=True, text=True, env=env, timeout=120)

    assert failed.returncode == 1, failed.stderr[-2000:]
    assert json.loads(failed.stdout)["ok"] is False
