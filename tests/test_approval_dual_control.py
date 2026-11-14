"""Regression tests for the approval findings M2 and M3 of the 2026-10-21 review.

M2: the requester of an orchestrator-created critical approval could count as
one of the two dual approvers, because only `created_by`/`proposer` were
checked and the orchestrator path recorded the requester as `actor` only.

M3: delegated break-glass (`target_actor` != caller) only needed a request-body
flag, so one owner could elevate a viewer token to owner and then use it as the
"second" dual approver.

Store-level tests run against both the SQLite stores and the PostgreSQL stores
(on an in-memory stand-in for `_PostgresJsonState`). The tests at the end run
the PostgreSQL stores against a real database when OMNIDESK_TEST_POSTGRES_DSN
is set (CI's PostgreSQL job) and are skipped otherwise.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from omnidesk_agent.config import AppConfig, PermissionConfig
from omnidesk_agent.core.models import ActionProposal
from omnidesk_agent.repositories.postgres_state import PostgresApprovalStore, PostgresBreakGlassStore, PostgresDualApprovalStore
from omnidesk_agent.security.admin_auth import AdminAuth
from omnidesk_agent.security.approval_required import ApprovalRequired
from omnidesk_agent.security.approval_store import ApprovalStore
from omnidesk_agent.security.break_glass import BreakGlassSession, BreakGlassStore, has_required_approvals
from omnidesk_agent.security.dual_approval import DualApprovalStore
from omnidesk_agent.security.idempotency import SQLiteSideEffectIdempotencyStore
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.server_routes.agent_routes import register_agent_routes, register_break_glass_routes

OWNER_TOKEN = "o" * 40
ADMIN_TOKEN = "a" * 40
OPERATOR_TOKEN = "p" * 40
VIEWER_TOKEN = "v" * 40
OWNER = "token:OMNIDESK_OWNER_TOKEN"
ADMIN = "token:OMNIDESK_ADMIN_TOKEN"
OPERATOR = "token:OMNIDESK_OPERATOR_TOKEN"
VIEWER = "token:OMNIDESK_VIEWER_TOKEN"
BACKENDS = ["sqlite", "postgres"]


class _MemoryState:
    """In-memory stand-in for _PostgresJsonState (only the methods these stores use)."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, dict[str, Any]]] = {}

    def put(self, namespace: str, key: str, value: dict[str, Any]) -> None:
        self.rows.setdefault(namespace, {})[key] = dict(value)

    def insert_once(self, namespace: str, key: str, value: dict[str, Any]) -> bool:
        bucket = self.rows.setdefault(namespace, {})
        if key in bucket:
            return False
        bucket[key] = dict(value)
        return True

    def get(self, namespace: str, key: str) -> dict[str, Any] | None:
        row = self.rows.get(namespace, {}).get(key)
        return dict(row) if row is not None else None

    def list(self, namespace: str, *, status: str | None = None, limit: int = 50, ascending: bool = False) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows.get(namespace, {}).values() if status is None or row.get("status") == status]

    def update_locked(self, namespace: str, key: str, updater) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        current = self.get(namespace, key)
        if current is None:
            raise KeyError(key)
        updated = updater(current)
        self.rows[namespace][key] = dict(updated)
        return updated


def _approval_stores(backend: str, tmp_path: Path) -> tuple[Any, Any]:
    if backend == "sqlite":
        dual = DualApprovalStore(tmp_path / "dual.sqlite3")
        return ApprovalStore(tmp_path / "approvals.sqlite3", dual_approval_store=dual), dual
    state = _MemoryState()
    dual = PostgresDualApprovalStore(state)
    return PostgresApprovalStore(state, ttl_seconds=600, dual_approval_store=dual), dual


def _break_glass_store(backend: str, tmp_path: Path) -> Any:
    if backend == "sqlite":
        return BreakGlassStore(tmp_path / "break.sqlite3", audit_log=tmp_path / "audit.jsonl")
    return PostgresBreakGlassStore(_MemoryState(), audit_log=tmp_path / "audit.jsonl")


def _expire(store: Any, session_id: str) -> None:
    if isinstance(store, BreakGlassStore):
        with sqlite3.connect(store.db_path) as con:
            con.execute("UPDATE break_glass_sessions SET expires_at=? WHERE session_id=?", (time.time() - 1, session_id))
    else:
        store.state.rows[store.namespace][session_id]["expires_at"] = time.time() - 1


def _critical(actor: str) -> ActionProposal:
    return ActionProposal(
        tool="shell",
        action="run",
        args={"cmd": "deploy"},
        risk="critical",
        reason="production change",
        source="local-api",
        actor=actor,
        run_id="run-1",
    )


def _permission_manager(tmp_path: Path, approvals: Any) -> PermissionManager:
    cfg = PermissionConfig(approval_mode="remote_approval", audit_log=tmp_path / "audit.log")
    return PermissionManager(cfg, approval_store=approvals)


# --- M2: requester recorded server-side and refused as a dual approver -------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_poc_dual_self_requester_from_actor_cannot_be_first_approver(backend: str, tmp_path: Path) -> None:
    approvals, dual = _approval_stores(backend, tmp_path)
    # Exact shape the orchestrator used to store: requester only present as `actor`.
    proposal = {"tool": "shell", "action": "run", "args": {}, "risk": "critical", "reason": "x", "source": "local-api",
                "actor": OWNER, "run_id": "r1", "requires_dual_approval": True}
    approval_id = approvals.create(proposal)

    with pytest.raises(PermissionError, match="creator cannot approve"):
        dual.approve(approval_id, OWNER)
    assert dual.approve(approval_id, ADMIN).ready is False
    assert dual.approve(approval_id, "token:SECOND_OWNER").ready is True


@pytest.mark.parametrize("backend", BACKENDS)
def test_orchestrator_path_records_trusted_requester(backend: str, tmp_path: Path) -> None:
    approvals, dual = _approval_stores(backend, tmp_path)
    manager = _permission_manager(tmp_path, approvals)

    with pytest.raises(ApprovalRequired) as raised:
        manager.verify(_critical(OWNER))
    stored = approvals.get(raised.value.approval_id)
    assert stored is not None
    assert stored["proposal"]["requires_dual_approval"] is True
    assert stored["proposal"]["created_by"] == OWNER
    assert stored["proposal"]["proposer"] == OWNER

    with pytest.raises(PermissionError, match="creator cannot approve"):
        dual.approve(raised.value.approval_id, OWNER)


@pytest.mark.parametrize("backend", BACKENDS)
def test_orchestrator_path_overrides_caller_supplied_requester(backend: str, tmp_path: Path) -> None:
    approvals, dual = _approval_stores(backend, tmp_path)
    manager = _permission_manager(tmp_path, approvals)
    forged = {**asdict(_critical(OWNER)), "created_by": "someone-else", "proposer": "someone-else"}

    with pytest.raises(ApprovalRequired) as raised:
        manager.verify(forged)
    stored = approvals.get(raised.value.approval_id)
    assert stored["proposal"]["created_by"] == OWNER
    assert stored["proposal"]["proposer"] == OWNER
    with pytest.raises(PermissionError):
        dual.approve(raised.value.approval_id, OWNER)


@pytest.mark.parametrize("backend", BACKENDS)
def test_dual_approval_without_requester_is_refused(backend: str, tmp_path: Path) -> None:
    approvals, dual = _approval_stores(backend, tmp_path)
    manager = _permission_manager(tmp_path, approvals)
    with pytest.raises(PermissionError, match="requester"):
        manager.verify(_critical(""))
    assert approvals.list() == []

    with pytest.raises(PermissionError, match="requester"):
        dual.open("a1", {"requires_dual_approval": True})
    with pytest.raises(PermissionError, match="requester"):
        approvals.create({"tool": "shell", "risk": "critical", "requires_dual_approval": True})
    if backend == "sqlite":
        # The SQLite store checks before writing. (PostgresApprovalStore is out of this
        # change's scope; a direct call there leaves a pending row without a dual record,
        # which can never be approved. Both production paths stamp a requester first.)
        assert approvals.list() == []
    for row in approvals.list():
        with pytest.raises((PermissionError, KeyError)):
            approvals.decide(row["id"], "approved")


@pytest.mark.parametrize("backend", BACKENDS)
def test_non_ascii_actor_is_compared_without_crashing(backend: str, tmp_path: Path) -> None:
    approvals, dual = _approval_stores(backend, tmp_path)
    approval_id = approvals.create({"tool": "shell", "requires_dual_approval": True, "created_by": "张三"})
    with pytest.raises(PermissionError, match="creator cannot approve"):
        dual.approve(approval_id, "张三")
    assert dual.approve(approval_id, "李四").ready is False
    with pytest.raises(PermissionError, match="distinct"):
        dual.approve(approval_id, "李四")


# --- M3: delegated break-glass needs a real second owner -----------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_poc_breakglass_store_refuses_single_owner_delegation(backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", OWNER_TOKEN)
    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", VIEWER_TOKEN)
    for name in ("OMNIDESK_ADMIN_TOKEN", "OMNIDESK_OPERATOR_TOKEN", "OMNIDESK_OWNER_ACTOR", "OMNIDESK_VIEWER_ACTOR"):
        monkeypatch.delenv(name, raising=False)
    store = _break_glass_store(backend, tmp_path)
    auth = AdminAuth(allowed_ips=[], break_glass_store=store, break_glass_enabled=True)

    with pytest.raises(PermissionError, match="second owner"):
        store.open(session_id="s1", actor=VIEWER, approved_by=OWNER, reason="outage restore", ttl_seconds=3600)
    with pytest.raises(KeyError):
        store.get("s1")
    elevated = auth.verify_headers(Headers({"authorization": "Bearer " + VIEWER_TOKEN, "x-omnidesk-break-glass-session": "s1"}), "1.2.3.4", "owner")
    assert elevated.ok is False
    assert elevated.role == "viewer"


@pytest.mark.parametrize("backend", BACKENDS)
def test_delegated_request_is_inert_until_a_distinct_second_owner_approves(backend: str, tmp_path: Path) -> None:
    store = _break_glass_store(backend, tmp_path)
    pending = store.request_delegated(session_id="s1", actor="viewer", requested_by="owner-a", reason="restore access", ttl_seconds=60)
    assert pending.active is False
    assert pending.status == "pending_second_approval"
    assert store.get("s1").status == "pending_second_approval"
    with pytest.raises(PermissionError, match="not active"):
        store.assert_active("s1", actor="viewer")

    with pytest.raises(PermissionError, match="distinct from the requesting owner"):
        store.approve("s1", approver="owner-a")
    with pytest.raises(PermissionError, match="elevated actor cannot approve"):
        store.approve("s1", approver="viewer")
    assert store.get("s1").active is False

    approved = store.approve("s1", approver="owner-b")
    assert approved.active is True
    assert approved.second_approver == "owner-b"
    assert approved.expires_at > time.time() + 50
    assert store.assert_active("s1", actor="viewer").session_id == "s1"
    with pytest.raises(PermissionError, match="actor mismatch"):
        store.assert_active("s1", actor="mallory")
    with pytest.raises(PermissionError, match="not waiting"):
        store.approve("s1", approver="owner-c")

    events = [json.loads(line)["event"] for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events == ["break_glass.request", "break_glass.approve"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_revoked_or_expired_requests_cannot_be_approved(backend: str, tmp_path: Path) -> None:
    store = _break_glass_store(backend, tmp_path)
    store.request_delegated(session_id="revoked", actor="viewer", requested_by="owner-a", reason="restore access")
    store.revoke("revoked", revoked_by="owner-a")
    with pytest.raises(PermissionError, match="revoked"):
        store.approve("revoked", approver="owner-b")

    store.request_delegated(session_id="late", actor="viewer", requested_by="owner-a", reason="restore access", approval_window_seconds=1)
    _expire(store, "late")
    with pytest.raises(PermissionError, match="expired"):
        store.approve("late", approver="owner-b")

    with pytest.raises(ValueError, match="self-targeted"):
        store.request_delegated(session_id="self", actor="owner-a", requested_by="owner-a", reason="restore access")
    with pytest.raises(ValueError, match="already exists"):
        store.request_delegated(session_id="late", actor="viewer", requested_by="owner-a", reason="restore access")
    store.open(session_id="own", actor="owner-a", approved_by="owner-a", reason="restore access")
    with pytest.raises(ValueError, match="already exists"):
        store.open(session_id="own", actor="owner-a", approved_by="owner-a", reason="restore access")
    with pytest.raises(KeyError):
        store.approve("missing", approver="owner-b")


def test_sqlite_single_owner_sessions_written_before_upgrade_stop_working(tmp_path: Path) -> None:
    db = tmp_path / "break.sqlite3"
    now = time.time()
    with sqlite3.connect(db) as con:
        con.execute(
            """
            CREATE TABLE break_glass_sessions (
              session_id TEXT PRIMARY KEY, actor TEXT NOT NULL, reason TEXT NOT NULL, approved_by TEXT NOT NULL,
              created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL, metadata_json TEXT NOT NULL
            )
            """
        )
        con.execute("INSERT INTO break_glass_sessions VALUES ('old-delegated', 'viewer', 'r', 'owner-a', ?, ?, NULL, '{}')", (now, now + 600))
        con.execute("INSERT INTO break_glass_sessions VALUES ('old-self', 'owner-a', 'r', 'owner-a', ?, ?, NULL, '{}')", (now, now + 600))

    store = BreakGlassStore(db, audit_log=tmp_path / "audit.jsonl")
    legacy = store.get("old-delegated")
    assert legacy.status == "legacy_unapproved"
    assert legacy.active is False
    with pytest.raises(PermissionError):
        store.assert_active("old-delegated", actor="viewer")
    with pytest.raises(PermissionError, match="not waiting"):
        store.approve("old-delegated", approver="owner-b")
    assert store.get("old-self").active is True


def test_sqlite_migration_is_safe_when_instances_start_together(tmp_path: Path) -> None:
    for round_ in range(5):
        db = tmp_path / f"break-{round_}.sqlite3"
        with sqlite3.connect(db) as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(
                "CREATE TABLE break_glass_sessions (session_id TEXT PRIMARY KEY, actor TEXT NOT NULL, reason TEXT NOT NULL, "
                "approved_by TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL, metadata_json TEXT NOT NULL)"
            )
            con.execute("INSERT INTO break_glass_sessions VALUES ('old', 'viewer', 'r', 'owner-a', 0, 9999999999, NULL, '{}')")
        barrier = threading.Barrier(6)
        errors: list[str] = []

        def start() -> None:
            barrier.wait(timeout=5)
            try:
                BreakGlassStore(db, audit_log=tmp_path / "audit.jsonl")
            except Exception as exc:  # pragma: no cover - the failure being guarded against
                errors.append(repr(exc))

        threads = [threading.Thread(target=start) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert BreakGlassStore(db, audit_log=tmp_path / "audit.jsonl").get("old").status == "legacy_unapproved"


def test_postgres_single_owner_sessions_written_before_upgrade_stop_working(tmp_path: Path) -> None:
    store = PostgresBreakGlassStore(_MemoryState(), audit_log=tmp_path / "audit.jsonl")
    now = time.time()
    # Row shape written by the previous PostgresBreakGlassStore.open (no status field).
    for session_id, actor in (("old-delegated", "viewer"), ("old-self", "owner-a")):
        store.state.put(store.namespace, session_id, {
            "session_id": session_id, "actor": actor, "reason": "r", "approved_by": "owner-a",
            "created_at": now, "expires_at": now + 600, "revoked_at": None, "metadata": {},
        })

    legacy = store.get("old-delegated")
    assert legacy.status == "legacy_unapproved"
    assert legacy.active is False
    with pytest.raises(PermissionError):
        store.assert_active("old-delegated", actor="viewer")
    with pytest.raises(PermissionError, match="not waiting"):
        store.approve("old-delegated", approver="owner-b")
    assert store.assert_active("old-self", actor="owner-a").active is True


def test_admin_auth_refuses_delegated_sessions_without_second_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    class UnupgradedStore:
        """A store that still returns single-owner delegated sessions as active."""

        def assert_active(self, session_id: str, *, actor: str) -> BreakGlassSession:
            return BreakGlassSession(session_id, actor, "r", time.time() + 60, "owner-a", True)

    assert has_required_approvals(BreakGlassSession("s", "viewer", "r", 0.0, "owner-a", True)) is False
    assert has_required_approvals(BreakGlassSession("s", "viewer", "r", 0.0, "owner-a", True, "active", "viewer")) is False
    assert has_required_approvals(BreakGlassSession("s", "viewer", "r", 0.0, "owner-a", True, "active", "owner-b")) is True
    assert has_required_approvals(BreakGlassSession("s", "owner-a", "r", 0.0, "owner-a", True)) is True

    monkeypatch.setenv("BG_TEST_VIEWER_TOKEN", VIEWER_TOKEN)
    for name in ("BG_TEST_UNSET_OPERATOR", "BG_TEST_UNSET_OWNER", "BG_TEST_UNSET_ADMIN"):
        monkeypatch.delenv(name, raising=False)
    auth = AdminAuth(
        viewer_token_env="BG_TEST_VIEWER_TOKEN",
        operator_token_env="BG_TEST_UNSET_OPERATOR",
        owner_token_env="BG_TEST_UNSET_OWNER",
        admin_token_env="BG_TEST_UNSET_ADMIN",
        break_glass_store=UnupgradedStore(),
        break_glass_enabled=True,
    )
    decision = auth.verify_headers(Headers({"authorization": "Bearer " + VIEWER_TOKEN, "x-omnidesk-break-glass-session": "s1"}), "127.0.0.1", "owner")
    assert decision.ok is False
    assert "second owner" in decision.reason


# --- routes with the real AdminAuth, on both backends ----------------------------------------


@pytest.fixture
def auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", OWNER_TOKEN)
    monkeypatch.setenv("OMNIDESK_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", VIEWER_TOKEN)
    for name in ("OMNIDESK_OWNER_ACTOR", "OMNIDESK_ADMIN_ACTOR", "OMNIDESK_OPERATOR_ACTOR", "OMNIDESK_VIEWER_ACTOR"):
        monkeypatch.delenv(name, raising=False)


class _Runtime:
    def __init__(self, backend: str, tmp_path: Path):
        self.approvals, self.dual_approval_store = _approval_stores(backend, tmp_path)
        self.break_glass_store = _break_glass_store(backend, tmp_path)
        self.admin_auth = AdminAuth(allowed_ips=[], break_glass_store=self.break_glass_store, break_glass_enabled=True)
        self.side_effect_idempotency = SQLiteSideEffectIdempotencyStore(tmp_path / "side_effect_idempotency.sqlite3")


def _admin_verifier(rt: _Runtime):  # type: ignore[no-untyped-def]
    async def admin(request: Request, role: str = "viewer"):
        decision = await rt.admin_auth.verify_request(request, required_role=role)
        if not decision.ok:
            raise HTTPException(status_code=403, detail=decision.reason)
        return decision

    return admin


def _client(backend: str, tmp_path: Path) -> tuple[TestClient, _Runtime]:
    cfg = AppConfig()
    cfg.permissions.break_glass_enabled = True
    rt = _Runtime(backend, tmp_path)
    app = FastAPI()
    register_agent_routes(app, cfg, rt, rt.approvals, _admin_verifier(rt))
    register_break_glass_routes(app, cfg, rt, _admin_verifier(rt))
    return TestClient(app), rt


def _headers(token: str, key: str | None = None, session: str | None = None) -> dict[str, str]:
    headers = {"authorization": f"Bearer {token}"}
    if key:
        headers["idempotency-key"] = key
    if session:
        headers["x-omnidesk-break-glass-session"] = session
    return headers


@pytest.mark.parametrize("backend", BACKENDS)
def test_post_approvals_records_authenticated_requester_and_rejects_named_actor(backend: str, tmp_path: Path, auth_env: None) -> None:
    client, rt = _client(backend, tmp_path)
    forged = client.post(
        "/approvals",
        headers=_headers(OPERATOR_TOKEN, "create-forged"),
        json={"tool": "shell", "risk": "critical", "requires_dual_approval": True, "created_by": OWNER},
    )
    assert forged.status_code == 422

    created = client.post(
        "/approvals",
        headers=_headers(OWNER_TOKEN, "create-1"),
        json={"tool": "shell", "risk": "critical", "requires_dual_approval": True, "actor": ADMIN},
    )
    assert created.status_code == 200
    approval_id = created.json()["id"]
    proposal = rt.approvals.get(approval_id)["proposal"]
    assert proposal["created_by"] == OWNER and proposal["proposer"] == OWNER

    by_creator = client.post(f"/approvals/{approval_id}/dual-approve", headers=_headers(OWNER_TOKEN, "dual-creator"))
    assert by_creator.status_code == 409
    by_named_actor = client.post(f"/approvals/{approval_id}/dual-approve", headers=_headers(ADMIN_TOKEN, "dual-actor"))
    assert by_named_actor.status_code == 409
    assert rt.dual_approval_store.status(approval_id).first_approver is None


@pytest.mark.parametrize("backend", BACKENDS)
def test_both_creation_paths_refuse_the_requester(backend: str, tmp_path: Path, auth_env: None) -> None:
    client, rt = _client(backend, tmp_path)
    manager = _permission_manager(tmp_path, rt.approvals)
    with pytest.raises(ApprovalRequired) as raised:
        manager.verify(_critical(OWNER))
    via_route = client.post(
        "/approvals",
        headers=_headers(OWNER_TOKEN, "create-2"),
        json={"tool": "shell", "risk": "critical", "requires_dual_approval": True},
    ).json()["id"]

    for approval_id in (raised.value.approval_id, via_route):
        response = client.post(f"/approvals/{approval_id}/dual-approve", headers=_headers(OWNER_TOKEN, f"self-{approval_id}"))
        assert response.status_code == 409, approval_id
        assert "creator" in response.json()["detail"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_break_glass_routes_need_a_second_owner_and_elevation_cannot_dual_approve(backend: str, tmp_path: Path, auth_env: None) -> None:
    client, rt = _client(backend, tmp_path)

    flag_only = client.post(
        "/admin/break-glass/open",
        headers=_headers(OWNER_TOKEN, "open-no-flag"),
        json={"target_actor": VIEWER, "reason": "outage restore"},
    )
    assert flag_only.status_code == 409

    opened = client.post(
        "/admin/break-glass/open",
        headers=_headers(OWNER_TOKEN, "open-1"),
        json={"session_id": "bg-1", "target_actor": VIEWER, "reason": "outage restore", "ttl_seconds": 600, "requires_dual_approval": True},
    )
    assert opened.status_code == 200
    body = opened.json()
    assert body["requires_second_approval"] is True
    assert body["session"]["active"] is False
    assert body["session"]["status"] == "pending_second_approval"

    # Pending: the viewer is not elevated yet.
    assert client.post("/approvals/x/dual-approve", headers=_headers(VIEWER_TOKEN, "early", session="bg-1")).status_code == 403

    # Neither the requester, the target, nor a lower role can supply the second approval.
    assert client.post("/admin/break-glass/approve/bg-1", headers=_headers(OWNER_TOKEN, "approve-self")).status_code == 409
    assert client.post("/admin/break-glass/approve/bg-1", headers=_headers(VIEWER_TOKEN, "approve-target")).status_code == 403
    assert client.post("/admin/break-glass/approve/bg-1", headers=_headers(OPERATOR_TOKEN, "approve-operator")).status_code == 403
    assert client.post("/admin/break-glass/approve/bg-1", headers=_headers(ADMIN_TOKEN)).status_code == 428
    assert rt.break_glass_store.get("bg-1").active is False

    approved = client.post("/admin/break-glass/approve/bg-1", headers=_headers(ADMIN_TOKEN, "approve-1"), json={"reason": "confirmed incident"})
    assert approved.status_code == 200
    assert approved.json()["session"]["active"] is True
    assert approved.json()["second_approver"] == ADMIN
    replay = client.post("/admin/break-glass/approve/bg-1", headers=_headers(ADMIN_TOKEN, "approve-1"), json={"reason": "confirmed incident"})
    assert replay.json() == approved.json()

    elevated = rt.admin_auth.verify_headers(Headers(_headers(VIEWER_TOKEN, session="bg-1")), "1.2.3.4", "owner")
    assert elevated.ok is True and elevated.role == "owner" and elevated.break_glass_session_id == "bg-1"
    # Elevation still serves ordinary owner reads.
    assert client.get("/admin/break-glass/status/bg-1", headers=_headers(VIEWER_TOKEN, session="bg-1")).status_code == 200

    # The PoC shape: one real owner approves, the elevated viewer tries to be the second approver.
    critical = client.post(
        "/approvals",
        headers=_headers(OPERATOR_TOKEN, "create-critical"),
        json={"tool": "shell", "risk": "critical", "requires_dual_approval": True},
    ).json()["id"]
    first = client.post(f"/approvals/{critical}/dual-approve", headers=_headers(OWNER_TOKEN, "dual-owner"))
    assert first.status_code == 200 and first.json()["dual_approval"]["ready"] is False
    via_elevation = client.post(f"/approvals/{critical}/dual-approve", headers=_headers(VIEWER_TOKEN, "dual-elevated", session="bg-1"))
    assert via_elevation.status_code == 403
    assert "break-glass" in via_elevation.json()["detail"]
    assert rt.dual_approval_store.status(critical).ready is False

    chained_open = client.post(
        "/admin/break-glass/open",
        headers=_headers(VIEWER_TOKEN, "chain-open", session="bg-1"),
        json={"target_actor": OPERATOR, "reason": "chain elevation", "requires_dual_approval": True},
    )
    assert chained_open.status_code == 403
    other = client.post(
        "/admin/break-glass/open",
        headers=_headers(OWNER_TOKEN, "open-2"),
        json={"session_id": "bg-2", "target_actor": OPERATOR, "reason": "second request", "requires_dual_approval": True},
    )
    assert other.status_code == 200
    chained_approve = client.post("/admin/break-glass/approve/bg-2", headers=_headers(VIEWER_TOKEN, "chain-approve", session="bg-1"))
    assert chained_approve.status_code == 403
    assert rt.break_glass_store.get("bg-2").active is False

    revoked = client.post("/admin/break-glass/revoke/bg-1", headers=_headers(OWNER_TOKEN, "revoke-1"), json={})
    assert revoked.status_code == 200
    assert rt.admin_auth.verify_headers(Headers(_headers(VIEWER_TOKEN, session="bg-1")), "1.2.3.4", "owner").ok is False


@pytest.mark.parametrize("backend", BACKENDS)
def test_break_glass_approve_route_requires_enabled_policy(backend: str, tmp_path: Path, auth_env: None) -> None:
    client, rt = _client(backend, tmp_path)
    rt.break_glass_store.request_delegated(session_id="bg-1", actor=VIEWER, requested_by=OWNER, reason="restore access")
    app = FastAPI()
    register_break_glass_routes(app, AppConfig(), rt, _admin_verifier(rt))
    disabled = TestClient(app).post("/admin/break-glass/approve/bg-1", headers=_headers(ADMIN_TOKEN, "approve-disabled"))
    assert disabled.status_code == 403
    assert rt.break_glass_store.get("bg-1").active is False
    assert client.post("/admin/break-glass/approve/missing", headers=_headers(ADMIN_TOKEN, "approve-missing")).status_code == 404


def test_delegated_open_refused_when_store_cannot_hold_pending_requests(tmp_path: Path, auth_env: None) -> None:
    class SelfOnlyStore:
        def __init__(self, inner: BreakGlassStore):
            self.open = inner.open
            self.get = inner.get
            self.assert_active = inner.assert_active

    client, rt = _client("sqlite", tmp_path)
    rt.break_glass_store = SelfOnlyStore(rt.break_glass_store)
    delegated = client.post(
        "/admin/break-glass/open",
        headers=_headers(OWNER_TOKEN, "custom-open"),
        json={"target_actor": VIEWER, "reason": "outage restore", "requires_dual_approval": True},
    )
    assert delegated.status_code == 409
    assert client.post("/admin/break-glass/approve/any", headers=_headers(ADMIN_TOKEN, "custom-approve")).status_code == 409


# --- PostgreSQL stores on a real database (CI PostgreSQL job) ---------------------------------


@pytest.fixture
def postgres_factory(tmp_path: Path):  # type: ignore[no-untyped-def]
    dsn = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("OMNIDESK_TEST_POSTGRES_DSN is not configured")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from omnidesk_agent.repositories.postgres import PostgresRepositoryFactory

    schema = "approval_dual_control_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    factory = PostgresRepositoryFactory(make_conninfo(dsn, options=f"-csearch_path={schema}"), pool_size=2)
    try:
        yield factory
    finally:
        factory.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_database_dual_approval_refuses_requester(postgres_factory, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    dual = postgres_factory.dual_approval_store()
    approvals = postgres_factory.approval_store(ttl_seconds=600, dual_approval_store=dual)
    with pytest.raises(ApprovalRequired) as raised:
        _permission_manager(tmp_path, approvals).verify(_critical(OWNER))
    approval_id = raised.value.approval_id
    assert approvals.get(approval_id)["proposal"]["created_by"] == OWNER
    with pytest.raises(PermissionError, match="creator cannot approve"):
        dual.approve(approval_id, OWNER)
    assert dual.approve(approval_id, ADMIN).ready is False
    assert dual.approve(approval_id, "token:SECOND_OWNER").ready is True


def test_postgres_database_break_glass_needs_second_owner(postgres_factory, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    store = postgres_factory.break_glass_store(audit_log=tmp_path / "audit.jsonl")
    with pytest.raises(PermissionError, match="second owner"):
        store.open(session_id="pg-single", actor="viewer", approved_by="owner-a", reason="outage restore")
    store.request_delegated(session_id="pg-1", actor="viewer", requested_by="owner-a", reason="outage restore", ttl_seconds=60)
    with pytest.raises(PermissionError, match="not active"):
        store.assert_active("pg-1", actor="viewer")
    with pytest.raises(PermissionError, match="distinct from the requesting owner"):
        store.approve("pg-1", approver="owner-a")
    with pytest.raises(PermissionError, match="elevated actor"):
        store.approve("pg-1", approver="viewer")
    assert store.approve("pg-1", approver="owner-b").second_approver == "owner-b"
    assert store.assert_active("pg-1", actor="viewer").active is True
    with pytest.raises(PermissionError, match="not waiting"):
        store.approve("pg-1", approver="owner-c")
    store.revoke("pg-1", revoked_by="owner-a")
    with pytest.raises(PermissionError, match="not active"):
        store.assert_active("pg-1", actor="viewer")
