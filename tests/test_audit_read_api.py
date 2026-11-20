"""The audit view's read API: auditors and owners only, organization-scoped, redacted, paged, with chain status.

Web Admin's audit view had no API: #256 only gave the agent loop a way to
write entries. Reading the audit log is itself sensitive, so this pins who may
read it (an auditor role that cannot operate anything, plus owners), that one
organization never sees another's entries, that credentials and content stay
out of the answer, and that the answer says whether the chain still verifies.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omnidesk_agent.config import AppConfig

TOKENS = {"viewer": "v" * 40, "auditor": "u" * 40, "operator": "p" * 40, "owner": "o" * 40}


@pytest.fixture()
def app(tmp_path, monkeypatch):
    for role, token in TOKENS.items():
        monkeypatch.setenv(f"OMNIDESK_{role.upper()}_TOKEN", token)
        monkeypatch.setenv(f"OMNIDESK_{role.upper()}_ACTOR", f"{role}-a")
    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "ws"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "state" / "audit.log"
    cfg.learning.growth_plan_file = tmp_path / "growth.json"
    cfg.channels.gmail.credentials_file = tmp_path / "g" / "c.json"
    cfg.channels.gmail.token_file = tmp_path / "g" / "t.json"
    cfg.gateway.admin_allowed_ips = ["testclient", "127.0.0.1"]
    cfg.plugins.enabled = False
    cfg.ensure_dirs()
    from omnidesk_agent.server import create_app

    application = create_app(cfg)
    record = application.state.runtime.permissions.record_event
    for i in range(5):
        record("agent_step_finished", {"actor": "operator-a", "run_id": f"run-{i}", "status": "done"})
    return application


def _get(client, role: str, **params):
    return client.get("/admin/audit/events", headers={"authorization": f"Bearer {TOKENS[role]}"}, params=params)


def test_only_auditors_and_owners_read_the_audit_logs(app):
    with TestClient(app) as client:
        assert _get(client, "auditor").status_code == 200
        assert _get(client, "owner").status_code == 200
        assert _get(client, "viewer").status_code == 403
        assert _get(client, "operator").status_code == 403  # an operator outranks a viewer, not an auditor


def test_an_auditor_cannot_operate(app):
    with TestClient(app) as client:
        headers = {"authorization": f"Bearer {TOKENS['auditor']}"}
        assert client.get("/admin/status", headers=headers).status_code == 200  # viewer pages work
        assert client.get("/admin/jobs", headers=headers).status_code == 403  # operator pages do not


def test_pages_follow_the_chain_in_order_without_gaps(app):
    seen: list[int] = []
    with TestClient(app) as client:
        after = 0
        while True:
            page = _get(client, "auditor", after_seq=after, limit=2).json()
            seen += [e["seq"] for e in page["events"]]
            if page["next_after_seq"] is None:
                break
            after = page["next_after_seq"]

    # Every entry exactly once, in order, across page boundaries.
    assert len(seen) >= 5 and seen == list(range(seen[0], seen[0] + len(seen)))


def test_content_and_credentials_stay_out_of_the_answer(app):
    app.state.runtime.permissions.record_event(
        "agent_turn_planned",
        {
            "actor": "operator-a",
            "args": {"text": "customer letter", "text_sha256": "ab" * 32, "text_length": 15},
            "note": "retry with Authorization: Bearer sk-live-abcdefghijklmnop",
            "api_key": "k-123",
            "text": "the model's full reply",
        },
    )
    with TestClient(app) as client:
        events = _get(client, "owner", limit=500).json()["events"]

    planned = next(e for e in events if e["event"] == "agent_turn_planned")["payload"]
    assert planned["args"] == {"text_sha256": "ab" * 32, "text_length": 15} and planned["args_withheld"] is True
    assert planned["text"] == "[WITHHELD]" and planned["api_key"] == "[REDACTED]"
    assert "sk-live-abcdefghijklmnop" not in planned["note"]


def test_each_organization_sees_only_its_own_entries(app, monkeypatch):
    rt = app.state.runtime
    rt.permissions.record_event("agent_step_finished", {"actor": "alice", "run_id": "run-alice"})
    orgs = {"alice": "org_a", "auditor-a": "org_a"}
    monkeypatch.setattr(rt.app_sync, "_organization_for_actor", lambda actor: orgs.get(actor, "org_default"), raising=False)

    with TestClient(app) as client:
        org_a = _get(client, "auditor", limit=500).json()
        default = _get(client, "owner", limit=500).json()

    assert org_a["organization"] == "org_a"
    assert [e["payload"]["run_id"] for e in org_a["events"]] == ["run-alice"]
    assert "run-alice" not in {e["payload"].get("run_id") for e in default["events"]}
    assert len(default["events"]) >= 5


def test_the_admin_log_can_be_read_too(app):
    with TestClient(app) as client:
        _get(client, "viewer")  # a refused request is on the admin record
        page = _get(client, "owner", log="admin", limit=500).json()

    assert page["log"] == "admin"
    assert any(e["event"] == "admin_auth" and e["payload"].get("ok") is False for e in page["events"])


def test_the_answer_says_whether_the_chain_verifies(app):
    monitor = app.state.audit_monitor
    log = Path(app.state.runtime.permissions.cfg.audit_log)
    with TestClient(app) as client:
        assert monitor.check_once() is True
        fine = _get(client, "auditor").json()["verification"]

        lines = log.read_bytes().splitlines(keepends=True)
        lines[2] = lines[2].replace(b"run-1", b"run-X")  # edit an entry inside the chain
        log.write_bytes(b"".join(lines))
        monitor._points.clear()
        assert monitor.check_once() is False
        broken = _get(client, "auditor").json()["verification"]

    assert fine["status"] == "ok" and fine["last_verified_seq"] is not None and fine["error"] is None
    assert broken["status"] == "failed" and broken["error"]


def test_bad_parameters_are_refused(app):
    with TestClient(app) as client:
        assert _get(client, "owner", log="other").status_code == 422
        assert _get(client, "owner", limit=501).status_code == 422
        assert _get(client, "owner", after_seq=-1).status_code == 422


def test_web_admin_reads_with_an_auditor_session(app):
    with TestClient(app) as client:
        opened = client.post("/admin/session/login", headers={"authorization": f"Bearer {TOKENS['auditor']}"}).json()
        page = client.get("/admin/audit/events", headers={"authorization": f"Bearer {opened['session_token']}"})

    assert opened["role"] == "auditor"
    assert page.status_code == 200 and page.json()["organization"] == opened["organization"]
