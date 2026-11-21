"""Admin sessions: short-lived, revocable, stored only as an HMAC, and bound to their role.

Web Admin kept the raw long-lived gateway bearer token in its cookie, and
logout only cleared the cookie, so a copied cookie kept working against the
gateway. These tests pin what replaces it:
- a session acts with exactly the role of the token that opened it;
- the token is never stored or logged;
- a row forged or edited without the audit key does not authenticate;
- logout and revoke take effect at once;
- logins are limited per actor;
- a login that cannot be audited does not stand.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnidesk_agent.config import AppConfig
from omnidesk_agent.security.admin_auth import AdminAuthDecision
from omnidesk_agent.security.admin_sessions import (
    MAX_ACTIVE_SESSIONS_PER_ACTOR,
    MAX_LOGINS_PER_MINUTE,
    AdminSessionLimit,
    SQLiteAdminSessionStore,
)
from omnidesk_agent.security.audit_errors import AuditIntegrityError
from omnidesk_agent.server_routes.admin_session_routes import register_admin_session_routes

TOKENS = {"viewer": "v" * 40, "operator": "p" * 40, "owner": "o" * 40}
KEY = b"k" * 32


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
    application.state.state_dir = tmp_path / "state"
    return application


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _login(client, role: str = "operator", **body):
    return client.post("/admin/session/login", headers=_bearer(TOKENS[role]), json=body or None)


def test_a_role_token_opens_a_session_that_acts_with_that_role(app):
    with TestClient(app) as client:
        opened = _login(client, "viewer", label="laptop")
        token = opened.json()["session_token"]

        identity = client.get("/admin/session/identity", headers=_bearer(token)).json()
        operator_route = client.get("/admin/jobs", headers=_bearer(token))

    assert opened.status_code == 200 and token.startswith("omds_")
    assert opened.headers["cache-control"] == "no-store"
    assert opened.json()["role"] == "viewer" and opened.json()["label"] == "laptop"
    assert identity["actor"] == "viewer-a" and identity["role"] == "viewer"
    assert operator_route.status_code == 403  # the session never exceeds the token's role


def test_the_session_token_is_never_stored_or_logged(app):
    with TestClient(app) as client:
        opened = _login(client).json()
        token = opened["session_token"]
        assert client.get("/admin/jobs", headers=_bearer(token)).status_code == 200

    state = app.state.state_dir
    stored = (state / "admin_sessions.sqlite3").read_bytes()
    audit = (state / "admin_auth_audit.jsonl").read_text(encoding="utf-8")
    assert token.encode() not in stored and hashlib.sha256(token.encode()).hexdigest().encode() not in stored
    assert token not in audit
    assert opened["session_id"] in audit  # the login and the request are on the record by session id
    assert '"admin_session.login"' in audit


def test_a_row_forged_or_edited_without_the_key_does_not_authenticate(app):
    db = app.state.state_dir / "admin_sessions.sqlite3"
    with TestClient(app) as client:
        token = _login(client, "viewer").json()["session_token"]
        forged = "omds_" + "x" * 43
        with sqlite3.connect(db) as con:
            # Raise the real session to owner, and add an owner session for a chosen token.
            con.execute("UPDATE omnidesk_admin_sessions SET role = 'owner'")
            con.execute(
                "INSERT INTO omnidesk_admin_sessions VALUES ('ses_forged', ?, 'owner-a', 'owner', 'org_default', '', 0, 4102444800, NULL, 'seal')",
                (hashlib.sha256(forged.encode()).hexdigest(),),
            )

        edited = client.get("/admin/session/identity", headers=_bearer(token))
        injected = client.get("/admin/session/identity", headers=_bearer(forged))

    assert edited.status_code == 403 and injected.status_code == 403


def test_logout_and_revoke_take_effect_at_once(app):
    with TestClient(app) as client:
        first = _login(client).json()["session_token"]
        second = _login(client).json()["session_token"]
        owners = _login(client, "owner").json()

        # Logout ends only the session making the request.
        assert client.post("/admin/session/revoke", headers=_bearer(first), json={}).json()["revoked"] == 1
        assert client.get("/admin/jobs", headers=_bearer(first)).status_code == 403
        assert client.get("/admin/jobs", headers=_bearer(second)).status_code == 200

        # An operator cannot touch someone else's sessions, and gets one answer for theirs and missing ones.
        assert client.post("/admin/session/revoke", headers=_bearer(second), json={"session_id": owners["session_id"]}).status_code == 404
        assert client.post("/admin/session/revoke", headers=_bearer(second), json={"session_id": "ses_missing"}).status_code == 404
        assert client.post("/admin/session/revoke", headers=_bearer(second), json={"all": True, "actor": "owner-a"}).status_code == 403

        # An owner can end all of an actor's sessions, for example after a leak.
        ended = client.post("/admin/session/revoke", headers=_bearer(owners["session_token"]), json={"all": True, "actor": "operator-a"}).json()
        assert ended["revoked"] == 1 and ended["audit"] == "recorded"
        assert client.get("/admin/jobs", headers=_bearer(second)).status_code == 403

        # Logout needs a session; a role token has none to end.
        assert client.post("/admin/session/revoke", headers=_bearer(TOKENS["operator"]), json={}).status_code == 422


def test_a_session_cannot_open_another_session(app):
    with TestClient(app) as client:
        token = _login(client).json()["session_token"]
        chained = client.post("/admin/session/login", headers=_bearer(token))

    assert chained.status_code == 403


def test_logins_are_limited_per_actor(app):
    with TestClient(app) as client:
        for _ in range(MAX_LOGINS_PER_MINUTE):
            assert _login(client).status_code == 200
        limited = _login(client)
        other_actor = _login(client, "viewer")

    assert limited.status_code == 429 and 1 <= int(limited.headers["retry-after"]) <= 60
    assert other_actor.status_code == 200


def test_live_sessions_per_actor_are_capped(tmp_path):
    clock = [1_000_000.0]
    store = SQLiteAdminSessionStore(tmp_path / "s.sqlite3", raw_key=KEY, clock=lambda: clock[0])
    for _ in range(MAX_ACTIVE_SESSIONS_PER_ACTOR):
        store.create(actor="a", role="operator", organization="org_default")
        clock[0] += 61  # stay under the per-minute limit
    with pytest.raises(AdminSessionLimit) as refused:
        store.create(actor="a", role="operator", organization="org_default")

    assert refused.value.retry_after_seconds >= 1
    store.revoke_actor("a")
    assert store.create(actor="a", role="operator", organization="org_default")[0].startswith("omds_")


def test_sessions_expire(tmp_path):
    clock = [1_000_000.0]
    store = SQLiteAdminSessionStore(tmp_path / "s.sqlite3", raw_key=KEY, clock=lambda: clock[0])
    token, session = store.create(actor="a", role="viewer", organization="org_x", ttl_seconds=120)

    assert store.resolve(token) == session and session.organization == "org_x"
    clock[0] += 121
    assert store.resolve(token) is None


def test_a_store_with_another_key_cannot_read_the_sessions(tmp_path):
    token, _ = SQLiteAdminSessionStore(tmp_path / "s.sqlite3", raw_key=KEY).create(actor="a", role="owner", organization="org_default")

    assert SQLiteAdminSessionStore(tmp_path / "s.sqlite3", raw_key=b"z" * 32).resolve(token) is None


def test_a_login_that_cannot_be_audited_does_not_stand(app, monkeypatch):
    rt = app.state.runtime

    def broken(event, payload):
        raise AuditIntegrityError("admin audit log cannot be written")

    monkeypatch.setattr(rt.admin_auth, "audit_event", broken)
    with TestClient(app, raise_server_exceptions=False) as client:
        refused = _login(client)

    assert refused.status_code == 503
    store = rt.admin_auth.session_store
    assert store.revoke_actor("operator-a") == []  # the session it opened was revoked again


def test_revoking_never_waits_on_the_audit_log(app, monkeypatch):
    rt = app.state.runtime
    with TestClient(app) as client:
        token = _login(client).json()["session_token"]

        def broken(event, payload):
            raise AuditIntegrityError("admin audit log cannot be written")

        monkeypatch.setattr(rt.admin_auth, "audit_event", broken)
        ended = client.post("/admin/session/revoke", headers=_bearer(token), json={}).json()

    assert ended["revoked"] == 1 and ended["audit"] == "not recorded: AuditIntegrityError"
    assert rt.admin_auth.session_store.resolve(token) is None


def _bare_app(decision: AdminAuthDecision, rt) -> FastAPI:
    app = FastAPI()

    async def admin(request, role):
        return decision

    register_admin_session_routes(app, None, rt, admin)
    return app


def test_a_break_glass_elevation_cannot_open_a_session(tmp_path):
    store = SQLiteAdminSessionStore(tmp_path / "s.sqlite3", raw_key=KEY)
    rt = SimpleNamespace(admin_auth=SimpleNamespace(session_store=store, audit_event=lambda e, p: None))
    elevated = AdminAuthDecision(True, "break-glass session accepted for viewer", actor="op-a", role="owner", break_glass_session_id="bg-1", auth_method="token")
    with TestClient(_bare_app(elevated, rt)) as client:
        assert client.post("/admin/session/login").status_code == 403

    assert store.revoke_actor("op-a") == []


def test_the_routes_answer_501_without_a_session_store():
    rt = SimpleNamespace(admin_auth=SimpleNamespace(session_store=None))
    owner = AdminAuthDecision(True, "admin token accepted", actor="owner-a", role="owner", auth_method="token")
    with TestClient(_bare_app(owner, rt)) as client:
        assert client.post("/admin/session/login").status_code == 501
        assert client.post("/admin/session/revoke", json={"all": True}).status_code == 501


POSTGRES_DSN = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "")


@pytest.mark.skipif(not POSTGRES_DSN, reason="OMNIDESK_TEST_POSTGRES_DSN is not configured")
def test_postgres_sessions_are_shared_by_every_gateway_instance():
    pytest.importorskip("psycopg")
    from omnidesk_agent.repositories.postgres_pool import SharedPostgresConnectionPool
    from omnidesk_agent.security.admin_sessions import PostgresAdminSessionStore

    pools = [SharedPostgresConnectionPool(POSTGRES_DSN, max_size=2) for _ in range(2)]
    try:
        first, second = (PostgresAdminSessionStore(pool, raw_key=KEY) for pool in pools)
        actor = f"pg-test-{os.urandom(4).hex()}"
        token, session = first.create(actor=actor, role="operator", organization="org_default")

        assert second.resolve(token) == session  # another instance sees it
        assert second.revoke(session.session_id) is True
        assert first.resolve(token) is None  # and its revocation
        with pools[0].connection() as con, con.cursor() as cur:
            cur.execute("SELECT count(*) FROM omnidesk_admin_sessions WHERE lookup = %s", (hashlib.sha256(token.encode()).hexdigest(),))
            assert cur.fetchone()[0] == 0  # only the HMAC is stored
    finally:
        for pool in pools:
            pool.close()


def test_local_development_access_cannot_become_a_session(tmp_path, monkeypatch):
    # Codex review: with allow_local_admin_without_token and no role tokens, a
    # credential-free loopback request is the "local-development" owner. Login
    # would have turned that into a token usable from any host.
    for role in TOKENS:
        monkeypatch.delenv(f"OMNIDESK_{role.upper()}_TOKEN", raising=False)
    monkeypatch.delenv("OMNIDESK_ADMIN_TOKEN", raising=False)
    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "ws"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "state" / "audit.log"
    cfg.learning.growth_plan_file = tmp_path / "growth.json"
    cfg.channels.gmail.credentials_file = tmp_path / "g" / "c.json"
    cfg.channels.gmail.token_file = tmp_path / "g" / "t.json"
    cfg.gateway.admin_allowed_ips = ["testclient"]
    cfg.gateway.allow_local_admin_without_token = True
    cfg.plugins.enabled = False
    cfg.ensure_dirs()
    from omnidesk_agent.server import create_app

    with TestClient(create_app(cfg)) as client:
        assert client.get("/admin/session/identity").json()["actor"] == "local-development"  # the bypass itself works
        refused = client.post("/admin/session/login")

    assert refused.status_code == 403


def test_a_leaked_session_can_be_revoked_while_the_audit_log_is_down(app, monkeypatch):
    # Codex review: authentication failed closed when the admin audit log could
    # not take its entry, so a revoke was refused exactly when it was needed.
    rt = app.state.runtime
    with TestClient(app) as client:
        token = _login(client).json()["session_token"]

        def broken(*args, **kwargs):
            raise AuditIntegrityError("admin audit log cannot be written")

        monkeypatch.setattr(rt.admin_auth.audit_chain, "append", broken)
        assert client.get("/admin/jobs", headers=_bearer(token)).status_code == 403  # everything else fails closed
        ended = client.post("/admin/session/revoke", headers=_bearer(token), json={})

    assert ended.status_code == 200 and ended.json()["revoked"] == 1
    assert ended.json()["audit"].startswith("not recorded")
    assert rt.admin_auth.session_store.resolve(token) is None



def test_device_signed_requests_are_audited_without_secrets(tmp_path, monkeypatch):
    # gateway-clients' #231 replaced the sync-feed event with this hook: refused
    # signatures and verified state changes go to the chained admin audit log.
    from omnidesk_agent.security.admin_auth import AdminAuth
    from omnidesk_agent.security.audit_chain import read_audit_events, verify_audit_log

    monkeypatch.setenv("OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY", KEY.decode())
    log = tmp_path / "admin_auth_audit.jsonl"
    auth = AdminAuth(audit_log=log)
    auth.record_device_request(verified=False, device_id="dev-1", actor="alice@x", method="post", path="/app/tasks/t1/claim?sig=abc", reason="invalid device signature", device_type="desktop", client_host="10.0.0.5")
    auth.record_device_request(verified=True, device_id="dev-1", actor="alice@x", method="POST", path="/app/approvals/a1/decide", device_type="mobile")

    rejected, verified = [e for e in read_audit_events(log) if str(e.get("event", "")).startswith("device_request.")]
    assert rejected["event"] == "device_request.rejected" and rejected["reason"] == "invalid device signature"
    assert rejected["path"] == "/app/tasks/t1/claim" and rejected["method"] == "POST"  # no query string
    assert verified["event"] == "device_request.verified" and "reason" not in verified
    assert "sig=abc" not in log.read_text(encoding="utf-8")
    assert verify_audit_log(log, KEY).ok  # on the same tamper-evident chain
