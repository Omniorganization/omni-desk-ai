"""AppSync approvals are four-eyes: no self-approval, two approvers for critical actions.

Before this, decide_approval set "approved" on one decision from anyone the
route let through. It checked neither the requester nor the risk, so a single
device signature released a critical desktop action, even one the same actor
had requested. Security's rule for the agent's own approvals (#224) now holds
here too:
- the requester never approves their own request (withdrawing it is fine);
- a critical action needs two distinct approvers, neither the requester;
- any eligible approver's rejection is final.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from omnidesk_agent.appsync.strict_json_store import StrictJsonAppSyncStore
from omnidesk_agent.config import AppConfig
from omnidesk_agent.server import create_app

REQUESTER = "oscar-operator"
FIRST = "olivia-owner"
SECOND = "adam-admin"


@pytest.fixture(params=["json", "postgres"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    if request.param == "json":
        yield StrictJsonAppSyncStore(tmp_path / "appsync.json")
        return
    dsn = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("OMNIDESK_TEST_POSTGRES_DSN is not configured")
    from omnidesk_agent.appsync.migrated_postgres_store import MigratedMultiInstancePostgresAppSyncStore
    from omnidesk_agent.appsync.postgres_migrations import apply_appsync_migrations

    namespace = f"four_eyes_{uuid.uuid4().hex}"
    apply_appsync_migrations(dsn, namespace=namespace)
    pg = MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=namespace, pool_size=2)
    try:
        yield pg
    finally:
        pg.close()


def _request(store: Any, *, risk: str) -> dict[str, Any]:
    for actor in (REQUESTER, FIRST, SECOND):
        store.ensure_user(actor)
    conversation = store.create_conversation(actor=REQUESTER, title="ops")
    return store.add_message_and_task(actor=REQUESTER, conversation_id=conversation["conversation_id"], content="restart the build agent", requires_desktop_runtime=True, risk=risk)


def _task_status(store: Any, task_id: str) -> str:
    return str(store.get_task(task_id, actor=REQUESTER)["status"])


def test_the_requester_cannot_approve_their_own_request(store: Any) -> None:
    created = _request(store, risk="high")
    with pytest.raises(PermissionError, match="requester cannot approve"):
        store.decide_approval(approval_id=created["approval"]["approval_id"], actor=REQUESTER, decision="approved")
    assert _task_status(store, created["task"]["task_id"]) == "blocked"
    # Someone else can.
    decided = store.decide_approval(approval_id=created["approval"]["approval_id"], actor=FIRST, decision="approved")
    assert decided["status"] == "approved" and decided["approved_by"] == [FIRST]
    assert _task_status(store, created["task"]["task_id"]) == "queued"


def test_the_requester_may_withdraw_their_request(store: Any) -> None:
    created = _request(store, risk="high")
    withdrawn = store.decide_approval(approval_id=created["approval"]["approval_id"], actor=REQUESTER, decision="rejected")
    assert withdrawn["status"] == "rejected"
    assert _task_status(store, created["task"]["task_id"]) == "cancelled"


def test_a_critical_action_needs_two_distinct_approvers(store: Any) -> None:
    created = _request(store, risk="critical")
    approval_id = created["approval"]["approval_id"]
    assert created["approval"]["required_approvals"] == 2
    first = store.decide_approval(approval_id=approval_id, actor=FIRST, decision="approved")
    # One approval is not enough: the task stays blocked.
    assert (first["status"], first["approved_by"]) == ("pending", [FIRST])
    assert _task_status(store, created["task"]["task_id"]) == "blocked"
    with pytest.raises(PermissionError, match="different approver"):
        store.decide_approval(approval_id=approval_id, actor=FIRST, decision="approved")
    with pytest.raises(PermissionError, match="requester cannot approve"):
        store.decide_approval(approval_id=approval_id, actor=REQUESTER, decision="approved")
    second = store.decide_approval(approval_id=approval_id, actor=SECOND, decision="approved")
    assert (second["status"], second["approved_by"], second["decided_by"]) == ("approved", [FIRST, SECOND], SECOND)
    assert _task_status(store, created["task"]["task_id"]) == "queued"


def test_one_rejection_ends_a_critical_approval(store: Any) -> None:
    created = _request(store, risk="critical")
    approval_id = created["approval"]["approval_id"]
    store.decide_approval(approval_id=approval_id, actor=FIRST, decision="approved")
    rejected = store.decide_approval(approval_id=approval_id, actor=SECOND, decision="rejected")
    assert rejected["status"] == "rejected"
    assert _task_status(store, created["task"]["task_id"]) == "cancelled"
    with pytest.raises(ValueError, match="already decided"):
        store.decide_approval(approval_id=approval_id, actor=SECOND, decision="approved")


def test_the_partial_approval_asks_the_mobile_approvers_for_the_second(store: Any) -> None:
    created = _request(store, risk="critical")
    store.decide_approval(approval_id=created["approval"]["approval_id"], actor=FIRST, decision="approved")
    titles = [item["title"] for item in store.list_notifications(actor=FIRST)]
    assert "Second approval required" in titles


def test_a_critical_approval_created_before_the_rule_still_needs_two(tmp_path: Path) -> None:
    store = StrictJsonAppSyncStore(tmp_path / "appsync.json")
    created = _request(store, risk="critical")
    # As stored by the previous release: no required_approvals of its own.
    store.approvals[created["approval"]["approval_id"]].required_approvals = 1
    assert store.decide_approval(approval_id=created["approval"]["approval_id"], actor=FIRST, decision="approved")["status"] == "pending"


# ------------------------------------------------------------------ routes ---


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "workspace"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.learning.growth_plan_file = tmp_path / "growth.json"
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.token_file = tmp_path / "google" / "token.json"
    cfg.gateway.admin_allowed_ips = ["testclient", "127.0.0.1", "::1"]
    return cfg


def test_self_approval_is_refused_by_the_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Actors are bound to tokens, so the owner token is one actor.
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("OMNIDESK_OWNER_ACTOR", FIRST)
    owner = {"authorization": "Bearer owner-token"}
    with TestClient(create_app(_cfg(tmp_path))) as client:
        conversation = client.post("/app/conversations", headers={**owner, "idempotency-key": "c1"}, json={"title": "ops"}).json()["conversation"]
        created = client.post(f"/app/conversations/{conversation['conversation_id']}/messages", headers={**owner, "idempotency-key": "m1"}, json={"content": "restart", "requires_desktop_runtime": True}).json()
        decided = client.post(f"/app/approvals/{created['approval']['approval_id']}/decide", headers={**owner, "idempotency-key": "d1"}, json={"decision": "approved"})
        assert decided.status_code == 403 and "requester cannot approve" in decided.text


def test_break_glass_is_never_one_of_the_four_eyes(store: Any) -> None:
    # An operator raised by break-glass may decide a single-approver action, never a critical one.
    critical = _request(store, risk="critical")
    with pytest.raises(PermissionError, match="break-glass"):
        store.decide_approval(approval_id=critical["approval"]["approval_id"], actor=FIRST, decision="approved", break_glass=True)
    assert store.get_task(critical["task"]["task_id"], actor=REQUESTER)["status"] == "blocked"
    medium = _request(store, risk="medium")
    assert store.decide_approval(approval_id=medium["approval"]["approval_id"], actor=FIRST, decision="approved", break_glass=True)["status"] == "approved"


def test_the_gateway_refuses_an_elevated_critical_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("OMNIDESK_OWNER_ACTOR", FIRST)
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", REQUESTER)
    app = create_app(_cfg(tmp_path))
    auth = app.state.runtime.admin_auth
    verify = auth.verify_request

    async def elevated(request: Any, required_role: str = "viewer") -> Any:
        # Stands in for AdminAuth's break-glass elevation of this request.
        decision = await verify(request, required_role=required_role)
        if request.headers.get("x-test-elevated"):
            decision.break_glass_session_id = "bg_test"
        return decision

    monkeypatch.setattr(auth, "verify_request", elevated)
    operator, owner = {"authorization": "Bearer operator-token"}, {"authorization": "Bearer owner-token", "x-test-elevated": "1"}
    with TestClient(app) as client:
        conversation = client.post("/app/conversations", headers={**operator, "idempotency-key": "c1"}, json={"title": "ops"}).json()["conversation"]
        path = f"/app/conversations/{conversation['conversation_id']}/messages"
        critical = client.post(path, headers={**operator, "idempotency-key": "m1"}, json={"content": "wipe the cache", "requires_desktop_runtime": True, "risk": "critical"}).json()
        medium = client.post(path, headers={**operator, "idempotency-key": "m2"}, json={"content": "read the log", "requires_desktop_runtime": True, "risk": "medium"}).json()
        refused = client.post(f"/app/approvals/{critical['approval']['approval_id']}/decide", headers={**owner, "idempotency-key": "d1"}, json={"decision": "approved"})
        assert refused.status_code == 403 and "break-glass" in refused.text
        accepted = client.post(f"/app/approvals/{medium['approval']['approval_id']}/decide", headers={**owner, "idempotency-key": "d2"}, json={"decision": "approved"})
        assert accepted.status_code == 200, accepted.text
