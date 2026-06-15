from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from omnidesk_agent.channels import http_client
from omnidesk_agent.channels.http_client import ChannelHttpClient
from omnidesk_agent.config import AppConfig, ChromeConfig
from omnidesk_agent.core.run_store import RunStore
from omnidesk_agent.sandbox.remote_runner import RemoteSandboxClient
from omnidesk_agent.sandbox.runner_server import ALLOWED_PREFIXES, _allowed
from omnidesk_agent.security.approval_store import ApprovalStore
from omnidesk_agent.security.command_policy import SAFE_CI_ALLOWED_PREFIXES
from omnidesk_agent.tools.browser import BrowserTool
from omnidesk_agent.validation.production import validate_production_config
from scripts import recover_resumes


class FakeResponse:
    status_code = 200
    text = '{"ok": true}'
    headers = {"x-request-id": "rid"}

    def json(self):
        return {"ok": True}


class FakeHttpxModule:
    class TimeoutException(Exception):
        pass

    class NetworkError(Exception):
        pass

    class TransportError(Exception):
        pass

    def __init__(self):
        self.calls = []

    def AsyncClient(self, timeout):
        module = self

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def request(self, method, url, headers=None, params=None, json=None):
                module.calls.append((method, url, headers or {}, params, json))
                return FakeResponse()

        return Client()


def test_run_resume_token_consumption_is_single_use(tmp_path: Path):
    store = RunStore(tmp_path / "runs.sqlite3")
    run_id = store.create({"message": "x"})
    token = store.save_waiting(run_id, {"goal": "g", "steps": []}, 0, [], "approval-1", {"tool": "shell"})
    store.consume_resume_token(run_id, token)
    assert store.get(run_id)["status"] == "resuming"
    with pytest.raises(PermissionError):
        store.consume_resume_token(run_id, token)


def test_resume_recovery_marks_stuck_resuming_runs(tmp_path: Path, capsys):
    db = tmp_path / "runs.sqlite3"
    store = RunStore(db)
    run_id = store.create({"message": "x"})
    token = store.save_waiting(run_id, {"goal": "g", "steps": []}, 0, [], "approval-1", {"tool": "shell"})
    store.consume_resume_token(run_id, token)
    assert [run["id"] for run in store.list_resuming(older_than_seconds=0)] == [run_id]

    assert recover_resumes.main(["--run-db", str(db), "--older-than-seconds", "0", "--mark-failed"]) == 0
    captured = capsys.readouterr()
    assert run_id in captured.out
    recovered = store.get(run_id)
    assert recovered["status"] == "resume_failed"
    assert recovered["results"][-1]["status"] == "resume_failed"


def test_approval_consume_is_single_use_and_bound(tmp_path: Path):
    store = ApprovalStore(tmp_path / "approvals.sqlite3")
    proposal = {"tool": "shell", "action": "run", "run_id": "r1", "scope_hash": "s1"}
    aid = store.create(proposal)
    store.decide(aid, "approved")
    consumed = store.consume_approved(aid, proposal, consumed_by_run_id="r1")
    assert consumed["status"] == "consumed"
    assert consumed["consumed_by_run_id"] == "r1"
    with pytest.raises(PermissionError):
        store.consume_approved(aid, proposal, consumed_by_run_id="r1")
    with pytest.raises(PermissionError):
        store.require_approved(aid, proposal)


def test_browser_tab_summary_redacts_non_allowed_origins():
    tool = BrowserTool(ChromeConfig(enabled=True, allowed_origins=["https://allowed.example"]))
    visible = tool._safe_tab_summary({"id": "1", "title": "OK", "url": "https://allowed.example/a"})
    hidden = tool._safe_tab_summary({"id": "2", "title": "Bank", "url": "https://bank.example/private"})
    assert visible["title"] == "OK"
    assert visible["redacted"] is False
    assert hidden == {"id": "2", "origin": "https://bank.example", "redacted": True}


def test_browser_list_tabs_spec_requires_approval():
    spec = BrowserTool(ChromeConfig()).spec()
    assert spec.actions["list_tabs"].requires_approval is True
    assert spec.actions["list_tabs"].risk == "medium"


def test_production_browser_requires_dedicated_profile(monkeypatch):
    cfg = AppConfig()
    cfg.gateway.public_base_url = "https://prod.example"
    cfg.channels.chrome.enabled = True
    cfg.channels.chrome.allowed_origins = ["https://allowed.example"]
    cfg.capabilities.browser.enabled = True
    cfg.memory_privacy.encrypt_at_rest = True
    cfg.sandbox.docker_image = "python:3.11-slim@sha256:" + "1" * 63 + "2"
    env = {
        cfg.gateway.admin_token_env: "a" * 40,
        cfg.gateway.shared_secret_env: "b" * 40,
        cfg.memory_privacy.encryption_key_env: "c" * 40,
        "OMNIDESK_PLUGIN_SIGNING_SECRET": "d" * 40,
    }
    result = validate_production_config(cfg, env)
    assert any("dedicated_profile_dir" in issue for issue in result["issues"])
    cfg.channels.chrome.dedicated_profile_dir = Path("/var/lib/omnidesk/chrome-profile")
    result = validate_production_config(cfg, env)
    assert not any("dedicated_profile_dir" in issue for issue in result["issues"])


def test_channel_http_client_propagates_idempotency_headers(monkeypatch):
    async def run_case():
        module = FakeHttpxModule()
        monkeypatch.setattr(http_client, "httpx", module)
        await ChannelHttpClient(max_retries=0).post("https://provider.test", json={"a": 1}, idempotency_key="idem-1", channel="telegram")
        headers = module.calls[0][2]
        assert headers["X-Omnidesk-Idempotency-Key"] == "idem-1"
        assert headers["X-Omnidesk-Client-Request-Id"] == "idem-1"

    asyncio.run(run_case())


def test_shell_and_runner_allowlists_share_policy():
    assert ALLOWED_PREFIXES == SAFE_CI_ALLOWED_PREFIXES
    assert _allowed(["git", "log", "--oneline"])
    assert _allowed(["git", "ls-tree", "HEAD"])


def test_remote_sandbox_client_defaults_match_runner_limits(monkeypatch):
    for name in [
        "OMNIDESK_SANDBOX_CLIENT_MAX_FILES",
        "OMNIDESK_SANDBOX_CLIENT_MAX_BYTES",
        "OMNIDESK_SANDBOX_CLIENT_MAX_FILE_BYTES",
        "OMNIDESK_SANDBOX_MAX_ARCHIVE_FILES",
        "OMNIDESK_SANDBOX_MAX_ARCHIVE_BYTES",
        "OMNIDESK_SANDBOX_MAX_ARCHIVE_FILE_BYTES",
    ]:
        monkeypatch.delenv(name, raising=False)
    client = RemoteSandboxClient("http://runner")
    assert client.max_archive_files == 512
    assert client.max_archive_bytes == 2 * 1024 * 1024
    assert client.max_file_bytes == 1024 * 1024


def test_github_actions_pin_checker_passes_current_workflows():
    result = subprocess.run(
        [sys.executable, "scripts/check_github_actions_pinned.py", ".github/workflows"],
        cwd=Path.cwd(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_self_upgrade_state_machine_blocks_direct_canary():
    from omnidesk_agent.self_upgrade.state_machine import UpgradeStateMachine

    sm = UpgradeStateMachine()
    with pytest.raises(ValueError):
        sm.transition("p1", "PROPOSED", "CANARY", "skip gates")


def test_webhook_rate_limit_is_enforced_after_atomic_upsert(tmp_path: Path):
    from omnidesk_agent.security.webhook_security import WebhookSecurity, WebhookSecurityConfig

    sec = WebhookSecurity(tmp_path / "webhook.sqlite3", WebhookSecurityConfig(rate_limit_max_requests=1))
    sec._check_rate("telegram", "u1")
    with pytest.raises(PermissionError):
        sec._check_rate("telegram", "u1")
