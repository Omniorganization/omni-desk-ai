"""CLI behaviour the audit found broken: no way to approve or resume a run, exit 0
for failed runs, a literal ./~ directory from config paths, and readiness commands
that called a runtime without any model key ready.

The run/approve/resume tests drive `omnidesk_agent.cli.main` end to end with the
real runtime (rule planner, remote approval, SQLite stores in a temp workspace).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from omnidesk_agent import cli
from omnidesk_agent.config import AppConfig, load_config
from omnidesk_agent.onboarding import build_model_doctor_checks
from omnidesk_agent.validation.models import REQUIRED_TASKS, live_connectivity_test, validate_models


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    for name in ("OMNIDESK_ENV", "APP_ENV", "ENV", "KUBERNETES_SERVICE_HOST", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return home


def write_config(path: Path, **overrides) -> Path:
    data = {
        "llm": {"provider": "rule"},
        "workspace": {"root": "~/.omnidesk/workspace", "skills_dirs": ["~/.omnidesk/skills"], "plugins_dirs": ["~/.omnidesk/plugins"], "memory_db": "~/.omnidesk/memory.sqlite3"},
        "permissions": {"approval_mode": "remote_approval", "audit_log": "~/.omnidesk/audit.log"},
        "capabilities": {"files": {"enabled": True, "allow_write": True}},
        # Defaults are expanded at import time with the real home; keep every path ensure_dirs touches here.
        "learning": {"growth_plan_file": "~/.omnidesk/growth_plan.json"},
        "channels": {"gmail": {"credentials_file": "~/.omnidesk/gmail/credentials.json", "token_file": "~/.omnidesk/gmail/token.json"}},
        **overrides,
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def omnidesk(config: Path, *argv: str, capsys) -> tuple[int, dict]:
    old = sys.argv
    sys.argv = ["omnidesk", "--config", str(config), *argv]
    try:
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
    finally:
        sys.argv = old
    out = capsys.readouterr().out
    return int(exit_info.value.code or 0), json.loads(out)


def test_run_waits_then_approve_and_resume_complete_it(home, tmp_path, capsys):
    config = write_config(tmp_path / "config.yaml")

    code, paused = omnidesk(config, "run", "write file hello", capsys=capsys)
    assert code == cli.EXIT_WAITING_APPROVAL
    assert paused["status"] == "waiting_approval" and paused["proposal"]["tool"] == "files"

    code, approved = omnidesk(config, "approve", paused["approval_id"], "--reason", "reviewed", capsys=capsys)
    assert code == 0 and approved["status"] == "approved"
    assert approved["proposal"]["args"]["text"] == "write file hello"  # the approver saw the full content

    code, done = omnidesk(config, "resume", paused["run_id"], "--token", paused["resume_token"], capsys=capsys)
    assert code == cli.EXIT_COMPLETED and done["status"] == "completed"
    assert (home / ".omnidesk" / "workspace" / "note.txt").read_text(encoding="utf-8") == "write file hello"

    # The token is single use.
    code, again = omnidesk(config, "resume", paused["run_id"], "--token", paused["resume_token"], capsys=capsys)
    assert code == cli.EXIT_FAILED and again["status"] == "resume_denied"


def test_denied_approval_makes_the_resumed_run_fail(home, tmp_path, capsys):
    config = write_config(tmp_path / "config.yaml")
    _, paused = omnidesk(config, "run", "write file hello", capsys=capsys)

    code, denied = omnidesk(config, "deny", paused["approval_id"], capsys=capsys)
    assert code == 0 and denied["status"] == "denied"
    code, resumed = omnidesk(config, "resume", paused["run_id"], "--token", paused["resume_token"], capsys=capsys)

    assert code == cli.EXIT_FAILED and resumed["status"] == "approval_not_satisfied"
    assert not (home / ".omnidesk" / "workspace" / "note.txt").exists()


def test_a_failed_run_exits_non_zero(home, tmp_path, capsys):
    config = write_config(tmp_path / "config.yaml", capabilities={"files": {"enabled": True, "allow_write": False}})
    code, result = omnidesk(config, "run", "write file hello", capsys=capsys)
    assert code == cli.EXIT_FAILED and result["status"] == "failed"


def test_a_run_that_raises_prints_the_error_and_exits_non_zero(home, tmp_path, capsys):
    config = write_config(tmp_path / "config.yaml", llm={"provider": "openai"})
    # No OPENAI_API_KEY: the model planner cannot plan.
    code, result = omnidesk(config, "run", "summarize my notes", capsys=capsys)
    assert code == cli.EXIT_FAILED and result["status"] in {"error", "failed"}


def test_approve_refuses_dual_control_and_production(monkeypatch):
    class Store:
        def __init__(self, proposal):
            self.proposal = proposal
            self.decided = []

        def get(self, approval_id):
            return {"id": approval_id, "status": "pending", "proposal": self.proposal}

        def decide(self, approval_id, status, result=None):
            self.decided.append(status)
            return {"status": status}

    critical = Store({"tool": "shell", "risk": "critical", "requires_dual_approval": True})
    result = cli.decide_approval(AppConfig(), SimpleNamespace(approval_store=critical), "a1", approve=True)
    assert result["status"] == "refused" and "two different owners" in result["error"] and critical.decided == []

    monkeypatch.setenv("OMNIDESK_ENV", "production")
    plain = Store({"tool": "files", "risk": "high"})
    refused = cli.decide_approval(AppConfig(), SimpleNamespace(approval_store=plain), "a2", approve=True)
    assert refused["status"] == "refused" and plain.decided == []
    # Denying is always allowed: it can only stop an action.
    assert cli.decide_approval(AppConfig(), SimpleNamespace(approval_store=plain), "a2", approve=False)["status"] == "denied"


def test_run_exit_codes():
    assert cli.run_exit_code({"status": "completed"}) == 0
    assert cli.run_exit_code({"status": "waiting_approval"}) == 3
    assert cli.run_exit_code({"status": "approval_pending"}) == 3
    for status in ("failed", "error", "resume_denied", "approval_not_satisfied", "not_found", ""):
        assert cli.run_exit_code({"status": status}) == 1


def test_home_relative_config_paths_are_expanded_everywhere(home, tmp_path):
    config = write_config(tmp_path / "config.yaml")
    cfg = load_config(config)

    assert cfg.workspace.root == home / ".omnidesk" / "workspace"
    assert cfg.workspace.skills_dirs == [home / ".omnidesk" / "skills"]
    assert cfg.permissions.audit_log == home / ".omnidesk" / "audit.log"
    assert (home / ".omnidesk" / "workspace").is_dir()
    assert not (tmp_path / "~").exists()  # no literal ./~ tree in the working directory


def test_validate_models_reports_missing_keys_instead_of_ok(home, monkeypatch):
    def runtime():
        from omnidesk_agent.core.token_budget import TokenBudgetManager
        from omnidesk_agent.models.router import ModelRouter

        return SimpleNamespace(model_router=ModelRouter(AppConfig().models, TokenBudgetManager(home / "tb.sqlite3")))

    without = validate_models(runtime())
    assert without["ok"] is False
    missing = without["tasks"]["code"]["missing_credentials"]
    assert missing and all(without["credentials"][name] == "missing" for name in missing)
    assert without["task_coverage"]["code"] is False  # code routes only to keyed OpenAI profiles
    assert without["tasks"]["planner"]["usable"] == ["local"] and without["tasks"]["planner"]["without_key_check"] == ["local"]

    monkeypatch.setenv("OPENAI_API_KEY", "set-for-test")
    with_key = validate_models(runtime())
    assert with_key["ok"] is True and with_key["tasks"]["code"]["missing_credentials"] == []
    assert with_key["credentials"]["planner"] == "present" and with_key["credentials"]["local"] == "not_required"


def _model_runtime(cfg: AppConfig, home: Path) -> SimpleNamespace:
    from omnidesk_agent.core.token_budget import TokenBudgetManager
    from omnidesk_agent.models.router import ModelRouter

    return SimpleNamespace(model_router=ModelRouter(cfg.models, TokenBudgetManager(home / "tb.sqlite3")))


def _only_profile(name: str, profile: dict) -> AppConfig:
    return AppConfig.model_validate({"models": {"default": name, "profiles": {name: profile}, "routing": {task: name for task in REQUIRED_TASKS}}})


KEYLESS = {
    "bedrock_claude": {"provider": "bedrock", "model": "anthropic.claude-3-5-sonnet-20240620-v1:0", "region": "us-east-1"},
    "my_ollama": {"provider": "ollama", "model": "qwen2.5-coder:7b"},
}


@pytest.mark.parametrize("name", sorted(KEYLESS))
def test_a_provider_that_reads_no_key_variable_is_not_reported_missing(home, monkeypatch, name):
    # Bedrock (the AWS credential chain) and Ollama never read api_key_env, which defaults to
    # OPENAI_API_KEY. Trusting it made validate-models exit 1 and doctor block with
    # "Set OPENAI_API_KEY" for a working profile.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = _only_profile(name, KEYLESS[name])
    assert cfg.models.profiles[name].api_key_env == "OPENAI_API_KEY"  # inherited, never read

    report = validate_models(_model_runtime(cfg, home))
    assert report["ok"] is True and report["credentials"] == {name: "not_required"}
    assert {c.status for c in build_model_doctor_checks(cfg, required=True)} == {"warn"}  # usable, not contacted


def test_the_live_check_calls_a_bedrock_profile_instead_of_skipping_it(home, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    runtime = _model_runtime(_only_profile("bedrock_claude", KEYLESS["bedrock_claude"]), home)
    called = []

    async def complete(request):
        called.append(request.metadata["profile"])
        return SimpleNamespace(text="ok", provider="bedrock", model="m")

    monkeypatch.setattr(runtime.model_router, "complete", complete)
    live = asyncio.run(live_connectivity_test(runtime))
    assert called == ["bedrock_claude"] and live["results"]["bedrock_claude"]["ok"] is True


def test_the_example_bedrock_profile_names_no_key_variable():
    from omnidesk_agent.config import ModelProfileConfig

    example = yaml.safe_load((Path(__file__).resolve().parents[1] / "examples" / "config.yaml").read_text(encoding="utf-8"))
    profile = ModelProfileConfig.model_validate(example["models"]["profiles"]["bedrock_claude"])
    assert profile.provider == "bedrock" and profile.api_key_env is None


def test_a_chatgpt_plan_profile_is_checked_for_the_token_it_reads(home, monkeypatch):
    monkeypatch.delenv("CHATGPT_PLAN_ACCESS_TOKEN", raising=False)
    cfg = _only_profile("plan", {"provider": "chatgpt_plan", "model": "gpt-6.1-sol", "api_key_env": None})
    checks = build_model_doctor_checks(cfg, required=True)
    assert {c.status for c in checks} == {"blocked"} and all("CHATGPT_PLAN_ACCESS_TOKEN" in (c.fix or "") for c in checks)

    monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", "set-for-test")
    assert {c.status for c in build_model_doctor_checks(cfg, required=True)} == {"pass"}


def test_doctor_reports_the_model_key_state(home, monkeypatch):
    cfg = AppConfig()
    checks = {c.name: c for c in build_model_doctor_checks(cfg, required=True)}
    # planner falls back to the keyless local profile; vision has no keyless fallback.
    assert checks["model:planner"].status == "warn" and "OPENAI_API_KEY" in checks["model:planner"].detail
    assert checks["model:vision"].status == "blocked"

    monkeypatch.setenv("OPENAI_API_KEY", "set-for-test")
    assert {c.status for c in build_model_doctor_checks(cfg, required=True)} == {"pass"}

    rule = AppConfig.model_validate({"llm": {"provider": "rule"}})
    assert [c.status for c in build_model_doctor_checks(rule, required=True)] == ["pass"]
    monkeypatch.delenv("OPENAI_API_KEY")
    assert build_model_doctor_checks(cfg, required=False)[1].status == "warn"  # source-only never blocks on keys
    assert os.environ.get("OPENAI_API_KEY") is None
