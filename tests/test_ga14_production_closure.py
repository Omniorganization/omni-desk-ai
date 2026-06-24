from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from omnidesk_agent.config import AppConfig
from omnidesk_agent.observability_probe import probe_otlp_endpoint
from omnidesk_agent.repositories.health import check_repository_factory
from omnidesk_agent.repositories.runtime import storage_plan
from omnidesk_agent.repositories.sqlite import SQLiteRepositoryFactory
from omnidesk_agent.security.break_glass import BreakGlassStore
from omnidesk_agent.validation.production import validate_production_config


class _OTLPHandler(BaseHTTPRequestHandler):
    requests: list[bytes] = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("content-length", "0")))
        self.__class__.requests.append(body)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):  # noqa: D401
        return None


def test_storage_health_roundtrip_and_multi_instance_guard(tmp_path: Path) -> None:
    factory = SQLiteRepositoryFactory(tmp_path / "outbox.sqlite3")
    health = check_repository_factory(factory, live_write=True)
    assert health.ok is True
    assert health.transactional_outbox is True
    assert health.multi_instance_safe is False
    with pytest.raises(RuntimeError, match="requires storage.backend=postgres"):
        storage_plan(backend="sqlite", require_multi_instance_safe=True)


def test_break_glass_is_time_boxed_targeted_and_audited(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    store = BreakGlassStore(tmp_path / "breakglass.sqlite3", audit_log=audit)
    assert store.open(session_id="s0", actor="owner", approved_by="owner", reason="outage", ttl_seconds=60).active is True
    session = store.open(session_id="s1", actor="operator", approved_by="owner", reason="restore production", ttl_seconds=60)
    assert session.active is True
    assert store.assert_active("s1", actor="operator").session_id == "s1"
    store.revoke("s1", revoked_by="owner")
    with pytest.raises(PermissionError):
        store.assert_active("s1", actor="operator")
    lines = audit.read_text(encoding="utf-8").splitlines()
    assert any('"break_glass.open"' in line for line in lines)
    assert any('"break_glass.revoke"' in line for line in lines)


def test_otlp_probe_uses_runtime_exporter_wire_format() -> None:
    _OTLPHandler.requests.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OTLPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1/traces"
        result = probe_otlp_endpoint(url, timeout=2.0)
        assert result.ok is True
        assert _OTLPHandler.requests
        payload = json.loads(_OTLPHandler.requests[-1].decode("utf-8"))
        assert payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "production_closure.otlp_probe"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_production_config_requires_critical_dual_approval_and_break_glass_hmac() -> None:
    cfg = AppConfig()
    cfg.plugins.enabled = False
    cfg.channels.chrome.enabled = False
    cfg.memory_privacy.encrypt_at_rest = True
    cfg.storage.backend = "postgres"
    cfg.app_sync.backend = "postgres"
    cfg.api_resource_guard.backend = "postgres"
    cfg.sandbox.docker_image = "python:3.11-slim@sha256:" + "66f011380d0e49ed280c789fbd08ff0d40968ee7b665575489afa95c98196ab5"
    cfg.permissions.require_dual_approval_for_risks = []
    env = {
        "OMNIDESK_ENV": "production",
        "OMNIDESK_ADMIN_TOKEN": "x" * 40,
        "OMNIDESK_GATEWAY_SECRET": "x" * 40,
        "OMNIDESK_MEMORY_ENCRYPTION_KEY": "x" * 40,
        "OMNIDESK_POSTGRES_DSN": "postgresql://user:pass@db/omnidesk",
        "OMNIDESK_APPSYNC_POSTGRES_DSN": "postgresql://user:pass@db/omnidesk",
    }
    result = validate_production_config(cfg, env)
    assert "permissions.require_dual_approval_for_risks must include critical in production" in result["issues"]
    cfg.permissions.require_dual_approval_for_risks = ["critical"]
    cfg.permissions.break_glass_enabled = True
    result = validate_production_config(cfg, env)
    assert "audit checkpoint HMAC key is not configured: OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY" in result["issues"]
    env["OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY"] = "x" * 40
    assert validate_production_config(cfg, env)["ok"] is True


def test_ga14_contract_scripts_pass_current_tree() -> None:
    commands = [
        [sys.executable, "scripts/check_kubernetes_contract.py", "."],
        [sys.executable, "scripts/production_closure_drill.py", "--root", ".", "--contract-only"],
        [sys.executable, "scripts/check_enterprise_readiness.py", "."],
    ]
    for cmd in commands:
        result = subprocess.run(cmd, text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr + result.stdout
