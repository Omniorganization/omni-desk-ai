from __future__ import annotations

import json
import sys

import pytest

from omnidesk_agent import cli
from omnidesk_agent.config import AppConfig


class FakeProposal:
    def to_dict(self):
        return {"id": "p1", "status": "open"}


class FakeMemory:
    def add(self, text, tags=None):
        self.added = (text, tags)
        return 7

    def search(self, query):
        return [{"query": query}]

    def metrics_report(self, days=7):
        return {"days": days, "memories": 3}

    def retrieve_for_task(self, query, limit=5):
        return [{"query": query, "limit": limit}]


class FakeGovernance:
    def generate_artifact(self, proposal_id):
        return {"proposal_id": proposal_id, "artifact": True}

    def record_human_feedback(self, proposal_id, decision, reason):
        return {"proposal_id": proposal_id, "decision": decision, "reason": reason}

    async def evaluate_proposal(self, proposal_id):
        return {"proposal_id": proposal_id, "evaluated": True}


class FakeOrchestrator:
    async def handle_message(self, msg):
        return {"channel": msg.channel, "text": msg.text}


class FakeGmailOAuth:
    def run_local_flow(self):
        return {"access_token": "token", "refresh_token": "refresh"}


class FakeRuntime:
    def __init__(self, cfg):
        self.cfg = cfg
        self.memory = FakeMemory()
        self.governance = FakeGovernance()
        self.orchestrator = FakeOrchestrator()
        self.proposal_store = type("Store", (), {"list": lambda self, status=None: [FakeProposal()]})()
        self.learning_job = type("Learning", (), {"run": lambda self, days=7: {"days": days}})()
        self.adapters = {"gmail": type("Gmail", (), {"oauth": FakeGmailOAuth()})()}

    def status(self):
        return {"ok": True}


def _run_cli(monkeypatch, capsys, args):
    monkeypatch.setattr(cli, "load_config", lambda path: AppConfig())
    monkeypatch.setattr(cli, "OmniDeskRuntime", FakeRuntime)
    monkeypatch.setattr(sys, "argv", ["omnidesk", "--config", "ignored.yaml", *args])
    cli.main()
    return capsys.readouterr().out


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["doctor", "--profile", "source-only"], '"summary"'),
        (["onboard", "--single-mac-ga-lab"], '"doctor_summary"'),
        (["evidence", "doctor"], '"required_categories"'),
        (["channel", "onboard", "slack"], '"pairing_required"'),
        (["device", "pair", "desktop-1"], '"challenge_required"'),
        (["app", "connect", "Slack"], '"foreground_confirmation_required"'),
        (["gmail-auth"], '"token_saved": true'),
        (["learning-report", "--days", "3"], '"days": 3'),
        (["metrics", "--days", "4"], '"memories": 3'),
        (["experience-search", "quota", "--limit", "2"], '"limit": 2'),
        (["upgrade-proposals"], '"status": "open"'),
        (["upgrade-artifact", "p1"], '"artifact": true'),
        (["upgrade-feedback", "p1", "rejected", "--reason", "risky"], '"reason": "risky"'),
        (["upgrade-evaluate", "p1"], '"evaluated": true'),
        (["run", "hello"], '"text": "hello"'),
        (["search", "invoice"], '"query": "invoice"'),
    ],
)
def test_cli_runtime_commands(monkeypatch, capsys, args, expected):
    assert expected in _run_cli(monkeypatch, capsys, args)


def test_cli_remember_prints_record_id(monkeypatch, capsys):
    output = _run_cli(monkeypatch, capsys, ["remember", "check stock", "--tags", "sales,inventory"])
    assert "remembered experience #7" in output


def test_cli_production_check_redacts_sensitive_validator_issues(monkeypatch, capsys):
    cfg = AppConfig()
    raw_issues = [
        "gateway shared secret must be at least 32 chars: OMNIDESK_GATEWAY_SECRET",
        "api resource guard postgres dsn is not configured: OMNIDESK_POSTGRES_DSN",
        "literal actual-secret-value from validator context",
    ]

    monkeypatch.setattr(cli, "load_config", lambda path, ensure_dirs=True: cfg)
    monkeypatch.setattr(
        cli,
        "validate_production_config",
        lambda cfg: {"ok": False, "production": True, "issues": raw_issues},
    )
    monkeypatch.setattr(sys, "argv", ["omnidesk", "--config", "ignored.yaml", "production-check"])

    with pytest.raises(SystemExit):
        cli.main()

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["issue_count"] == 3
    assert payload["issues_redacted"] is True
    assert payload["issue_categories"] == ["api_resource_guard", "gateway", "production_config"]
    assert "issues" not in payload
    assert "OMNIDESK_GATEWAY_SECRET" not in output
    assert "OMNIDESK_POSTGRES_DSN" not in output
    assert "actual-secret-value" not in output


def test_cli_serve_applies_host_and_port_before_creating_app(monkeypatch):
    captured = {}

    def fake_create_app(cfg):
        captured["host"] = cfg.gateway.host
        captured["port"] = cfg.gateway.port
        return object()

    def fake_run(app, host, port):
        captured["run"] = (host, port)

    monkeypatch.setattr(cli, "load_config", lambda path: AppConfig())
    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["omnidesk", "serve", "--host", "0.0.0.0", "--port", "18888"])

    cli.main()

    assert captured == {"host": "0.0.0.0", "port": 18888, "run": ("0.0.0.0", 18888)}
