"""GET /approvals shows each caller only what their role and organization may see.

Regression test for the 2026-11-21 audit critic: the route needed only the
viewer role and returned every actor's approvals with the full proposal
content, typed text included.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnidesk_agent.config import AppConfig
from omnidesk_agent.security.approval_required import ApprovalRequired
from omnidesk_agent.tools.base import ToolContext, bound_content, proposal

TOKENS = {"viewer": "v" * 40, "operator": "p" * 40, "owner": "o" * 40}
SECRET_TEXT = "typed: correct horse battery staple"


@pytest.fixture()
def app_and_runtime(tmp_path, monkeypatch):
    for role, token in TOKENS.items():
        monkeypatch.setenv(f"OMNIDESK_{role.upper()}_TOKEN", token)
        monkeypatch.setenv(f"OMNIDESK_{role.upper()}_ACTOR", f"{role}-a")
    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "ws"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.permissions.approval_mode = "remote_approval"
    cfg.learning.growth_plan_file = tmp_path / "growth.json"
    cfg.channels.gmail.credentials_file = tmp_path / "g" / "c.json"
    cfg.channels.gmail.token_file = tmp_path / "g" / "t.json"
    cfg.gateway.admin_allowed_ips = ["testclient", "127.0.0.1"]
    cfg.plugins.enabled = False
    cfg.ensure_dirs()
    from omnidesk_agent.server import create_app

    app = create_app(cfg)
    rt = app.state.runtime
    # The operator asks to type a secret; it waits for approval.
    ctx = ToolContext(permissions=rt.permissions, source="local-api", actor="operator-a", run_id="r1", plan_id="p1", step_index=0)
    with pytest.raises(ApprovalRequired):
        rt.permissions.verify(proposal("computer", "type_text", {**bound_content("text", SECRET_TEXT), "expected_result": "log in"}, "high", "type", ctx))
    return app, rt


def _list(client, role: str):
    response = client.get("/approvals", headers={"authorization": f"Bearer {TOKENS[role]}"})
    assert response.status_code == 200, response.text
    return response.json()["approvals"]


def test_viewers_see_what_is_asked_but_not_the_content(app_and_runtime):
    app, _rt = app_and_runtime
    with TestClient(app) as client:
        [item] = _list(client, "viewer")

    assert item["status"] == "pending" and item["proposal"]["tool"] == "computer" and item["proposal"]["risk"] == "high"
    assert SECRET_TEXT not in str(item)
    # No digest or length either: an unsalted digest of short typed text is brute-forced in milliseconds.
    assert hashlib.sha256(SECRET_TEXT.encode()).hexdigest() not in str(item)
    assert "args" not in item["proposal"] and item["proposal"]["args_withheld"] is True


@pytest.mark.parametrize("role", ["operator", "owner"])
def test_the_requester_and_owners_see_the_content(app_and_runtime, role):
    app, _rt = app_and_runtime
    with TestClient(app) as client:
        [item] = _list(client, role)

    assert item["proposal"]["args"]["text"] == SECRET_TEXT


def test_other_organizations_see_nothing(app_and_runtime):
    app, rt = app_and_runtime
    # The real configuration path, not a patched private resolver.
    rt.app_sync.configure_actor_organizations({"operator-a": "org-a", "owner-a": "org-b", "viewer-a": "org-b"})
    with TestClient(app) as client:
        assert _list(client, "owner") == []
        assert _list(client, "viewer") == []
        assert len(_list(client, "operator")) == 1


def test_an_operator_who_did_not_ask_gets_the_summary(app_and_runtime):
    # The fixture's only operator is also the requester; here someone else asks.
    app, rt = app_and_runtime
    ctx = ToolContext(permissions=rt.permissions, source="telegram", actor="telegram:42", run_id="r2", plan_id="p2", step_index=0)
    with pytest.raises(ApprovalRequired):
        rt.permissions.verify(proposal("computer", "type_text", {**bound_content("text", "042917"), "expected_result": "code"}, "high", "type", ctx))
    with TestClient(app) as client:
        as_operator = {item["proposal"].get("created_by") or item["proposal"].get("actor"): item for item in _list(client, "operator")}
        as_owner = {item["proposal"].get("created_by") or item["proposal"].get("actor"): item for item in _list(client, "owner")}

    assert "args" not in as_operator["telegram:42"]["proposal"] and "042917" not in str(as_operator["telegram:42"])
    assert as_owner["telegram:42"]["proposal"]["args"]["text"] == "042917"


def test_a_requester_whose_organization_cannot_be_resolved_is_shown_to_nobody(app_and_runtime, monkeypatch):
    app, rt = app_and_runtime

    def resolve(actor):
        if actor == "operator-a":
            raise RuntimeError("directory unavailable")
        return "org_default"

    monkeypatch.setattr(rt.app_sync, "_organization_for_actor", resolve)
    with TestClient(app) as client:
        assert _list(client, "owner") == []


def test_a_break_glass_decider_sees_what_they_decide():
    from omnidesk_agent.security.admin_auth import AdminAuthDecision
    from omnidesk_agent.server_routes.agent_routes import register_agent_routes

    row = {"id": "a1", "status": "pending", "proposal": {"tool": "computer", "action": "type_text", "risk": "high", "created_by": "telegram:42", "args": {"text": "042917"}}}
    approvals = SimpleNamespace(list=lambda status=None: [row])
    rt = SimpleNamespace(app_sync=None)

    async def admin(request, role):
        if role == "owner":  # the break-glass session verifies at owner level
            return AdminAuthDecision(True, "break-glass session accepted for owner", actor="op-a", role="owner", break_glass_session_id="bg-1")
        return AdminAuthDecision(True, "admin token accepted", actor="op-a", role="operator")

    app = FastAPI()
    register_agent_routes(app, SimpleNamespace(), rt, approvals, admin)
    with TestClient(app) as client:
        plain = client.get("/approvals").json()["approvals"][0]
        elevated = client.get("/approvals", headers={"x-omnidesk-break-glass-session": "bg-1"}).json()["approvals"][0]

    assert "args" not in plain["proposal"]
    assert elevated["proposal"]["args"]["text"] == "042917"
