"""Cancelling a run and the emergency stop over HTTP.

agent-core's #260 adds Orchestrator.cancel and the runtime emergency stop;
these routes expose them. Stopping must stay easy: an operator can engage the
stop, and an unwritable audit log never blocks it. Starting again must stay
deliberate: only an owner, never through break-glass, and never without an
audit entry. An operator cancels only the runs started with their own
identity, and cannot tell someone else's run from a missing one.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnidesk_agent.config import AppConfig
from omnidesk_agent.security.admin_auth import AdminAuthDecision
from omnidesk_agent.security.audit_errors import AuditIntegrityError
from omnidesk_agent.server_routes.run_control_routes import register_run_control_routes

TOKENS = {"viewer": "v" * 40, "operator": "p" * 40, "owner": "o" * 40}


@dataclass(frozen=True)
class _State:
    engaged: bool
    reason: str = ""
    actor: str = ""

    def to_dict(self) -> dict:
        return {"engaged": self.engaged, "reason": self.reason, "actor": self.actor}


class FakeStop:
    """The interface of core.run_control.EmergencyStop from #260, kept in memory."""

    def __init__(self) -> None:
        self.state = _State(False)

    def status(self) -> _State:
        return self.state

    def engage(self, *, reason: str, actor: str) -> _State:
        self.state = _State(True, reason, actor)
        return self.state

    def release(self) -> _State:
        self.state = _State(False)
        return self.state


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
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.learning.growth_plan_file = tmp_path / "growth.json"
    cfg.channels.gmail.credentials_file = tmp_path / "g" / "c.json"
    cfg.channels.gmail.token_file = tmp_path / "g" / "t.json"
    cfg.gateway.admin_allowed_ips = ["testclient", "127.0.0.1"]
    cfg.plugins.enabled = False
    cfg.ensure_dirs()
    from omnidesk_agent.server import create_app

    application = create_app(cfg)
    rt = application.state.runtime
    cancels: list[tuple[str, str, str]] = []

    async def cancel(run_id: str, *, reason: str = "", actor: str = "operator") -> dict:
        cancels.append((run_id, reason, actor))
        return {"ok": True, "status": "cancelled", "run_id": run_id}

    monkeypatch.setattr(rt.orchestrator, "cancel", cancel, raising=False)
    monkeypatch.setattr(rt, "emergency_stop", FakeStop(), raising=False)

    # Pass every stop change through the real record_event (name, size and
    # redaction checks, fail closed) and keep a copy to assert on.
    audited: list[tuple[str, dict]] = []
    real_record = rt.permissions.record_event

    def record(event: str, payload: dict) -> None:
        real_record(event, payload)
        audited.append((event, payload))

    monkeypatch.setattr(rt.permissions, "record_event", record)
    application.state.cancels = cancels
    application.state.audited = audited
    return application


def _h(role: str, key: str | None = None) -> dict[str, str]:
    headers = {"authorization": f"Bearer {TOKENS[role]}"}
    if key:
        headers["idempotency-key"] = key
    return headers


def _run(app, sender: str) -> str:
    return app.state.runtime.run_store.create({"channel": "local-api", "sender_id": sender, "text": "hi"})


def test_operator_cancels_only_runs_started_with_their_identity(app):
    own, other = _run(app, "operator-a"), _run(app, "whatsapp:+15550001")
    with TestClient(app) as client:
        done = client.post(f"/agent/runs/{own}/cancel", headers=_h("operator", "c-1"), json={"reason": "wrong order"})
        refused = client.post(f"/agent/runs/{other}/cancel", headers=_h("operator", "c-2"))
        missing = client.post("/agent/runs/no-such-run/cancel", headers=_h("operator", "c-3"))

    assert done.json()["status"] == "cancelled"
    # Someone else's run answers exactly like a missing one, so ids cannot be probed.
    assert refused.status_code == missing.status_code == 404
    assert refused.json() == missing.json()
    assert app.state.cancels == [(own, "wrong order", "operator-a")]


def test_owner_cancels_any_run_and_a_viewer_none(app):
    other = _run(app, "whatsapp:+15550001")
    with TestClient(app) as client:
        assert client.post(f"/agent/runs/{other}/cancel", headers=_h("viewer", "c-0")).status_code == 403
        assert client.post(f"/agent/runs/{other}/cancel", headers=_h("owner", "c-1")).status_code == 200

    assert app.state.cancels == [(other, "", "owner-a")]


def test_cancel_needs_an_idempotency_key_and_a_replay_does_not_cancel_twice(app):
    own = _run(app, "operator-a")
    with TestClient(app) as client:
        assert client.post(f"/agent/runs/{own}/cancel", headers=_h("operator")).status_code == 428
        first = client.post(f"/agent/runs/{own}/cancel", headers=_h("operator", "c-1")).json()
        replay = client.post(f"/agent/runs/{own}/cancel", headers=_h("operator", "c-1")).json()

    assert first == replay
    assert len(app.state.cancels) == 1


def test_an_erased_run_is_nobodys_run(app):
    # Erasure (agent-core #275) leaves in-flight runs as tombstones with
    # original_message {"erased": true}: a missing sender never matches.
    tombstone = app.state.runtime.run_store.create({"erased": True})
    with TestClient(app) as client:
        assert client.post(f"/agent/runs/{tombstone}/cancel", headers=_h("operator", "c-1")).status_code == 404

    assert app.state.cancels == []


def test_a_cancel_that_took_effect_is_reported_even_if_its_audit_entry_fails(app, monkeypatch):
    # cancel() writes the cancelled row first, then agent_run_cancelled.
    rt = app.state.runtime
    cancelled, untouched = _run(app, "operator-a"), _run(app, "operator-a")

    async def cancel(run_id: str, *, reason: str = "", actor: str = "operator") -> dict:
        if run_id == cancelled:
            rt.run_store.update(run_id, {"status": "cancelled"})
        raise AuditIntegrityError("audit chain cannot be written")

    monkeypatch.setattr(rt.orchestrator, "cancel", cancel, raising=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        done = client.post(f"/agent/runs/{cancelled}/cancel", headers=_h("operator", "c-1"))
        failed = client.post(f"/agent/runs/{untouched}/cancel", headers=_h("operator", "c-2"))

    assert done.status_code == 200
    assert done.json() == {"ok": True, "status": "cancelled", "run_id": cancelled, "audit_error": "AuditIntegrityError"}
    assert failed.status_code == 503


def test_an_operator_engages_the_stop_and_only_an_owner_releases_it(app):
    stop = app.state.runtime.emergency_stop
    with TestClient(app) as client:
        assert client.get("/admin/emergency-stop", headers=_h("viewer")).json()["engaged"] is False
        assert client.post("/admin/emergency-stop/engage", headers=_h("viewer", "s-0")).status_code == 403
        engaged = client.post(
            "/admin/emergency-stop/engage", headers=_h("operator", "s-1"), json={"reason": "replying to customers in a loop"}
        ).json()
        assert engaged["engaged"] is True and engaged["audit"] == "recorded"
        assert client.get("/admin/emergency-stop", headers=_h("viewer")).json()["actor"] == "operator-a"

        assert client.post("/admin/emergency-stop/release", headers=_h("operator", "r-0")).status_code == 403
        assert stop.status().engaged is True
        released = client.post("/admin/emergency-stop/release", headers=_h("owner", "r-1"), json={"reason": "loop fixed"}).json()

    assert released["engaged"] is False
    assert [(event, p["engaged"], p["actor"], p["via"]) for event, p in app.state.audited] == [
        ("agent_emergency_stop", True, "operator-a", "http"),
        ("agent_emergency_stop", False, "owner-a", "http"),
    ]


def test_a_broken_audit_log_never_blocks_the_stop_but_blocks_its_release(app, monkeypatch):
    rt = app.state.runtime

    def broken(event: str, payload: dict) -> None:
        raise AuditIntegrityError("audit chain cannot be written")

    monkeypatch.setattr(rt.permissions, "record_event", broken)
    with TestClient(app) as client:
        engaged = client.post("/admin/emergency-stop/engage", headers=_h("operator", "s-1")).json()
        refused = client.post("/admin/emergency-stop/release", headers=_h("owner", "r-1"))

    assert engaged["engaged"] is True and engaged["audit"] == "not recorded: AuditIntegrityError"
    assert refused.status_code == 503
    assert rt.emergency_stop.status().engaged is True


def test_releasing_a_stop_that_is_not_engaged_records_nothing(app):
    with TestClient(app) as client:
        assert client.post("/admin/emergency-stop/release", headers=_h("owner", "r-1")).json()["engaged"] is False

    assert app.state.audited == []


def _bare_app(decision: AdminAuthDecision, rt) -> FastAPI:
    app = FastAPI()

    async def admin(request, role):
        return decision

    register_run_control_routes(app, None, rt, admin)
    return app


def test_a_break_glass_elevation_cannot_release_the_stop():
    stop = FakeStop()
    stop.engage(reason="incident", actor="operator-a")
    recorded: list[str] = []
    rt = SimpleNamespace(emergency_stop=stop, permissions=SimpleNamespace(record_event=lambda event, payload: recorded.append(event)))
    elevated = AdminAuthDecision(True, "break-glass session accepted for owner", actor="operator-a", role="owner", break_glass_session_id="bg-1")
    with TestClient(_bare_app(elevated, rt)) as client:
        response = client.post("/admin/emergency-stop/release", headers={"idempotency-key": "r-1"})

    assert response.status_code == 403
    assert stop.status().engaged is True and recorded == []


def test_the_routes_answer_501_until_the_runtime_supports_them():
    owner = AdminAuthDecision(True, "admin token accepted", actor="owner-a", role="owner")
    rt = SimpleNamespace(orchestrator=SimpleNamespace(), permissions=SimpleNamespace())
    with TestClient(_bare_app(owner, rt)) as client:
        assert client.post("/agent/runs/r1/cancel", headers={"idempotency-key": "c-1"}).status_code == 501
        assert client.get("/admin/emergency-stop").status_code == 501
        assert client.post("/admin/emergency-stop/engage", headers={"idempotency-key": "s-1"}).status_code == 501
        assert client.post("/admin/emergency-stop/release", headers={"idempotency-key": "r-1"}).status_code == 501


def test_a_broken_idempotency_store_never_blocks_a_stop(app, monkeypatch):
    # Codex review: begin() needs a write transaction; an unavailable, read-only
    # or full store made engage fail before the stop ran.
    import sqlite3

    rt = app.state.runtime
    own = _run(app, "operator-a")

    def unavailable(**kwargs):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(rt.side_effect_idempotency, "begin", unavailable)
    with TestClient(app, raise_server_exceptions=False) as client:
        engaged = client.post("/admin/emergency-stop/engage", headers=_h("operator", "s-1"))
        cancelled = client.post(f"/agent/runs/{own}/cancel", headers=_h("operator", "c-1"))
        released = client.post("/admin/emergency-stop/release", headers=_h("owner", "r-1"))

    assert engaged.status_code == 200 and rt.emergency_stop.status().engaged is True
    assert cancelled.status_code == 200 and app.state.cancels == [(own, "", "operator-a")]
    # Giving capability back stays strict: without bookkeeping the release fails and the stop holds.
    assert released.status_code == 500 and rt.emergency_stop.status().engaged is True
