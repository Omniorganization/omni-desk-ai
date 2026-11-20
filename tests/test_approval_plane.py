"""Agent steps that wait for approval are decided in the clients' signed plane.

Before this, the orchestrator's approvals could be decided only through
/approvals/* and /agent/resume, which no client calls, and no client saw
them. Now every pause appears as an AppSync approval in Web Admin and on
mobile. The decision goes to security's ApprovalStore, which stays the ledger,
under its rules:
- owner only, never the requester;
- never break-glass for a critical step;
- two distinct owners for a critical step;
- the approver must be in the run's organization.
Only then is the run resumed, or ended on a rejection. Pushes carry no
content.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from omnidesk_agent.appsync.agent_approvals import LEDGER_ACTOR, AppSyncApprovalListener, decide_agent_approval, refuse_break_glass_for_critical
from omnidesk_agent.appsync.store import push_text
from omnidesk_agent.appsync.strict_json_store import StrictJsonAppSyncStore
from omnidesk_agent.config import AppConfig
from omnidesk_agent.security.approval_store import ApprovalStore
from omnidesk_agent.security.dual_approval import DualApprovalStore
from omnidesk_agent.server import create_app

REQUESTER = "telegram:42"
SECRET_ARG = "TOP-SECRET-CONTENT"


def _proposal(*, dual: bool = False, risk: str = "high", requester: str = REQUESTER) -> dict[str, Any]:
    proposal: dict[str, Any] = {"tool": "files", "action": "write", "args": {"path": "notes.txt", "content": SECRET_ARG}, "risk": risk, "scope_hash": "scope-1", "created_by": requester, "proposer": requester, "actor": requester}
    if dual:
        proposal["requires_dual_approval"] = True
    return proposal


def _event(ledger_id: str, proposal: dict[str, Any], *, organization: str | None = "org_default", requester: str = REQUESTER) -> dict[str, Any]:
    return {"run_id": "run_1", "approval_id": ledger_id, "proposal": proposal, "requester": requester, "channel": "telegram", "risk": proposal["risk"], "requires_dual_approval": bool(proposal.get("requires_dual_approval")), "organization": organization, "created_at": 1.0, "expires_at": None}


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

    namespace = f"plane_{uuid.uuid4().hex}"
    apply_appsync_migrations(dsn, namespace=namespace)
    pg = MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=namespace, pool_size=2)
    try:
        yield pg
    finally:
        pg.close()


# --------------------------------------------------------------- listener ---


def test_a_pause_appears_in_the_clients_plane_with_its_proposal(store: Any) -> None:
    listener = AppSyncApprovalListener(store)
    listener.on_approval_wait(_event("ledger_1", _proposal()))
    store.ensure_user("olivia-owner")
    [approval] = [a for a in store.bootstrap(actor="olivia-owner")["pending_approvals"] if a["approval_id"] == "agt_ledger_1"]
    assert approval["action"] == "Agent step files.write" and approval["requested_by"] == REQUESTER
    # Authenticated clients see the stored (redacted) proposal; the summary carries no args.
    assert approval["action_detail"]["proposal"]["args"]["path"] == "notes.txt"
    assert SECRET_ARG not in approval["action"]
    # The same pause reported twice is one approval.
    listener.on_approval_wait(_event("ledger_1", _proposal()))
    assert sum(1 for a in store.bootstrap(actor="olivia-owner")["pending_approvals"] if a["approval_id"] == "agt_ledger_1") == 1


def test_without_an_organization_nobody_is_told(store: Any) -> None:
    AppSyncApprovalListener(store).on_approval_wait(_event("ledger_2", _proposal(), organization=None))
    store.ensure_user("olivia-owner")
    assert not [a for a in store.bootstrap(actor="olivia-owner")["pending_approvals"] if a["approval_id"] == "agt_ledger_2"]


def test_pushes_carry_no_content() -> None:
    # Third parties (FCM, APNs) see a fixed text, plus tool.action and risk for agent steps.
    title, body = push_text("approval.requested", step="Agent step files.write (high)")
    assert (title, body) == ("Omni: approval required", "Agent step files.write (high) needs approval.")
    assert push_text("conversation.ask.completed") == ("Omni: new reply", "Open Omni to read it.")


def test_queued_pushes_never_hold_the_proposal_or_task_text(tmp_path: Path) -> None:
    store = StrictJsonAppSyncStore(tmp_path / "appsync.json")
    store.ensure_user("olivia-owner")
    store.register_device(actor="olivia-owner", device_id="dev_phone", device_type="mobile", name="p", platform="android", push_token="fcm-1")
    device = store.devices["dev_phone"]
    device.credential_status, device.trust_level = "verified", "challenge_verified"
    AppSyncApprovalListener(store).on_approval_wait(_event("ledger_3", _proposal()))
    conversation = store.create_conversation(actor="olivia-owner", title="ops")
    store.add_message_and_task(actor="olivia-owner", conversation_id=conversation["conversation_id"], content="move the secret plan to the shared drive", requires_desktop_runtime=True)
    queued = [(item.title, item.body) for item in store.push_outbox.values()]
    assert ("Omni: approval required", "Agent step files.write (high) needs approval.") in queued
    assert all(SECRET_ARG not in body and "secret plan" not in body for _, body in queued)


# ------------------------------------------------------- decide (helpers) ---


class FakeOrchestrator:
    def __init__(self) -> None:
        self.resumed: list[str] = []

    async def resume_by_approval(self, approval_id: str) -> dict[str, Any]:
        self.resumed.append(approval_id)
        return {"ok": True, "status": "completed", "run_id": "run_1", "approval_id": approval_id, "result": {"huge": "output"}}


def _stored(ledger: ApprovalStore, ledger_id: str) -> dict[str, Any]:
    record = ledger.get(ledger_id)
    assert record is not None
    return record


def _ledger(tmp_path: Path) -> ApprovalStore:
    return ApprovalStore(tmp_path / "approvals.sqlite3", dual_approval_store=DualApprovalStore(tmp_path / "dual.sqlite3"))


@pytest.mark.asyncio
async def test_break_glass_never_approves_a_critical_step(tmp_path: Path) -> None:
    store, ledger = StrictJsonAppSyncStore(tmp_path / "appsync.json"), _ledger(tmp_path)
    ledger_id = ledger.create(_proposal(dual=True, risk="critical"))
    AppSyncApprovalListener(store).on_approval_wait(_event(ledger_id, _stored(ledger, ledger_id)["proposal"]))
    with pytest.raises(PermissionError, match="break-glass"):
        await decide_agent_approval(store=store, approval_store=ledger, orchestrator=FakeOrchestrator(), approval_id=f"agt_{ledger_id}", actor="olivia-owner", decision="approved", reason=None, break_glass=True, idempotency_key=None, idempotency_payload=None)
    assert _stored(ledger, ledger_id)["status"] == "pending"
    # The same rule guards AppSync's own critical approvals.
    with pytest.raises(PermissionError, match="break-glass"):
        refuse_break_glass_for_critical({"required_approvals": 2}, break_glass=True)
    refuse_break_glass_for_critical({"required_approvals": 1}, break_glass=True)


# ------------------------------------------------------------ decide route ---


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


OWNER = {"authorization": "Bearer owner-token"}
SECOND_OWNER = {"authorization": "Bearer admin-token"}
OPERATOR = {"authorization": "Bearer operator-token"}


def _tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in (("OWNER", "owner"), ("ADMIN", "admin"), ("OPERATOR", "operator")):
        monkeypatch.setenv(f"OMNIDESK_{name}_TOKEN", f"{value}-token")
    monkeypatch.setenv("OMNIDESK_OWNER_ACTOR", "olivia-owner")
    monkeypatch.setenv("OMNIDESK_ADMIN_ACTOR", "adam-admin")
    monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", "oscar-operator")


@pytest.fixture
def plane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Any, ApprovalStore, FakeOrchestrator]]:
    _tokens(monkeypatch)
    app = create_app(_cfg(tmp_path))
    ledger, orchestrator = _ledger(tmp_path), FakeOrchestrator()
    app.state.runtime.approval_store = ledger
    app.state.runtime.orchestrator = orchestrator
    with TestClient(app) as client:
        yield client, app.state.runtime.app_sync, ledger, orchestrator


def _pause(plane: tuple[TestClient, Any, ApprovalStore, FakeOrchestrator], **kwargs: Any) -> str:
    _, store, ledger, _ = plane
    requester = kwargs.pop("requester", REQUESTER)
    organization = kwargs.pop("organization", "org_default")
    ledger_id = ledger.create(_proposal(requester=requester, **kwargs))
    AppSyncApprovalListener(store).on_approval_wait(_event(ledger_id, _stored(ledger, ledger_id)["proposal"], organization=organization, requester=requester))
    return ledger_id


def _decide(client: TestClient, ledger_id: str, auth: dict[str, str], decision: str, key: str) -> Any:
    return client.post(f"/app/approvals/agt_{ledger_id}/decide", headers={**auth, "idempotency-key": key}, json={"decision": decision, "reason": "reviewed"})


def test_an_owner_approves_and_the_run_resumes(plane: Any) -> None:
    client, _, ledger, orchestrator = plane
    ledger_id = _pause(plane)
    decided = _decide(client, ledger_id, OWNER, "approved", "a1")
    assert decided.status_code == 200, decided.text
    assert decided.json()["approval"]["status"] == "approved"
    # Only a summary of the run comes back, never its output.
    assert decided.json()["run"] == {"ok": True, "status": "completed", "run_id": "run_1", "approval_id": ledger_id}
    assert _stored(ledger, ledger_id)["status"] == "approved" and orchestrator.resumed == [ledger_id]


def test_an_operators_signed_device_cannot_approve(plane: Any) -> None:
    client, _, ledger, orchestrator = plane
    ledger_id = _pause(plane)
    assert _decide(client, ledger_id, OPERATOR, "approved", "o1").status_code == 403
    assert _stored(ledger, ledger_id)["status"] == "pending" and orchestrator.resumed == []


def test_the_requester_cannot_approve(plane: Any) -> None:
    client, _, ledger, _ = plane
    ledger_id = _pause(plane, requester="olivia-owner")
    refused = _decide(client, ledger_id, OWNER, "approved", "r1")
    assert refused.status_code == 403 and "requester cannot approve" in refused.text
    assert _stored(ledger, ledger_id)["status"] == "pending"


def test_an_approver_of_another_organization_is_refused(plane: Any) -> None:
    client, store, ledger, _ = plane
    store.ensure_user("bea-other", organization_id="org-b")
    ledger_id = _pause(plane, organization="org-b", requester="bea-other")
    assert _decide(client, ledger_id, OWNER, "approved", "x1").status_code == 403
    assert _stored(ledger, ledger_id)["status"] == "pending"


def test_a_critical_step_needs_two_distinct_owners(plane: Any) -> None:
    client, _, ledger, orchestrator = plane
    ledger_id = _pause(plane, dual=True, risk="critical")
    first = _decide(client, ledger_id, OWNER, "approved", "c1")
    assert first.status_code == 200 and first.json()["approval"]["status"] == "pending"
    assert _stored(ledger, ledger_id)["status"] == "pending" and orchestrator.resumed == []
    assert _decide(client, ledger_id, OWNER, "approved", "c2").status_code == 403
    second = _decide(client, ledger_id, SECOND_OWNER, "approved", "c3")
    assert second.status_code == 200 and second.json()["approval"]["status"] == "approved"
    assert _stored(ledger, ledger_id)["status"] == "approved" and orchestrator.resumed == [ledger_id]


def test_a_rejection_ends_the_run(plane: Any) -> None:
    client, _, ledger, orchestrator = plane
    ledger_id = _pause(plane)
    rejected = _decide(client, ledger_id, OWNER, "rejected", "d1")
    assert rejected.status_code == 200 and rejected.json()["approval"]["status"] == "rejected"
    # The ledger records the denial and resume_by_approval ends the run (agent-core #280).
    assert _stored(ledger, ledger_id)["status"] == "denied" and orchestrator.resumed == [ledger_id]


def test_a_retried_decision_replays_without_touching_the_ledger_again(plane: Any) -> None:
    client, _, ledger, orchestrator = plane
    ledger_id = _pause(plane)
    first = _decide(client, ledger_id, OWNER, "approved", "same-key")
    again = _decide(client, ledger_id, OWNER, "approved", "same-key")
    assert first.status_code == again.status_code == 200
    assert again.json()["approval"]["status"] == "approved"
    # resume_by_approval is idempotent on agent-core's side; the ledger was decided once.
    assert _stored(ledger, ledger_id)["status"] == "approved"


# ------------------------------------------------- failure between ledger and mirror ---


class FlakyMirror:
    """An AppSync store whose next decide_approval fails once, after the ledger was written."""

    def __init__(self, store: Any) -> None:
        self.store, self.fail_next = store, False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    def decide_approval(self, **kwargs: Any) -> Any:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("lost between the ledger and the mirror")
        return self.store.decide_approval(**kwargs)


def _plane_parts(tmp_path: Path, **proposal: Any) -> tuple[FlakyMirror, ApprovalStore, FakeOrchestrator, str]:
    store, ledger, orchestrator = StrictJsonAppSyncStore(tmp_path / "appsync.json"), _ledger(tmp_path), FakeOrchestrator()
    ledger_id = ledger.create(_proposal(**proposal))
    AppSyncApprovalListener(store).on_approval_wait(_event(ledger_id, _stored(ledger, ledger_id)["proposal"]))
    return FlakyMirror(store), ledger, orchestrator, ledger_id


async def _owner_decides(store: Any, ledger: ApprovalStore, orchestrator: FakeOrchestrator, ledger_id: str, *, actor: str = "olivia-owner", decision: str = "approved", key: str | None = "k1") -> dict[str, Any]:
    return await decide_agent_approval(store=store, approval_store=ledger, orchestrator=orchestrator, approval_id=f"agt_{ledger_id}", actor=actor, decision=decision, reason=None, break_glass=False, idempotency_key=key, idempotency_payload={"decision": decision} if key else None)


@pytest.mark.asyncio
async def test_a_retry_after_a_lost_mirror_finishes_the_decision(tmp_path: Path) -> None:
    store, ledger, orchestrator, ledger_id = _plane_parts(tmp_path)
    store.fail_next = True
    with pytest.raises(RuntimeError):
        await _owner_decides(store, ledger, orchestrator, ledger_id)
    # The ledger is decided, the mirror is not, and the run did not resume yet.
    assert _stored(ledger, ledger_id)["status"] == "approved" and orchestrator.resumed == []
    outcome = await _owner_decides(store, ledger, orchestrator, ledger_id)
    assert outcome["approval"]["status"] == "approved" and orchestrator.resumed == [ledger_id]


@pytest.mark.asyncio
async def test_a_lost_mirror_of_a_first_critical_approval_is_recovered(tmp_path: Path) -> None:
    store, ledger, orchestrator, ledger_id = _plane_parts(tmp_path, dual=True, risk="critical")
    store.fail_next = True
    with pytest.raises(RuntimeError):
        await _owner_decides(store, ledger, orchestrator, ledger_id)
    # The dual store holds the first approval; the retry counts it as done.
    retried = await _owner_decides(store, ledger, orchestrator, ledger_id)
    assert retried["approval"]["status"] == "pending" and retried["approval"]["approved_by"] == ["olivia-owner"]
    second = await _owner_decides(store, ledger, orchestrator, ledger_id, actor="adam-admin", key="k2")
    assert second["approval"]["status"] == "approved" and orchestrator.resumed == [ledger_id]


@pytest.mark.asyncio
async def test_a_keyless_retry_of_a_finished_decision_resumes_again_without_error(tmp_path: Path) -> None:
    store, ledger, orchestrator, ledger_id = _plane_parts(tmp_path)
    await _owner_decides(store, ledger, orchestrator, ledger_id)
    again = await _owner_decides(store, ledger, orchestrator, ledger_id, key=None)
    # resume_by_approval answers already_handled on agent-core's side; nothing runs twice.
    assert again["approval"]["status"] == "approved" and orchestrator.resumed == [ledger_id, ledger_id]


@pytest.mark.asyncio
async def test_another_owner_on_a_decided_approval_gets_a_conflict(tmp_path: Path) -> None:
    store, ledger, orchestrator, ledger_id = _plane_parts(tmp_path)
    await _owner_decides(store, ledger, orchestrator, ledger_id)
    with pytest.raises(ValueError, match="already decided"):
        await _owner_decides(store, ledger, orchestrator, ledger_id, actor="adam-admin", decision="rejected", key="k9")


# ------------------------------------------- a ledger decided outside the plane ---
# Security's own routes (/approvals/{id}/approve, /deny, /dual-approve) and the
# CLI decide the ledger directly and record ``decided_by``. The mirror must name
# who the ledger names, never whoever calls the plane next.


@pytest.fixture
def ledger_plane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Any, Any, FakeOrchestrator]]:
    """The plane beside security's /approvals/* routes, both on the runtime's own approval store."""
    _tokens(monkeypatch)
    app = create_app(_cfg(tmp_path))
    orchestrator = FakeOrchestrator()
    app.state.runtime.orchestrator = orchestrator
    with TestClient(app) as client:
        yield client, app.state.runtime.app_sync, app.state.runtime.approval_store, orchestrator


def _outside(client: TestClient, ledger_id: str, auth: dict[str, str], route: str, key: str) -> Any:
    return client.post(f"/approvals/{ledger_id}/{route}", headers={**auth, "idempotency-key": key}, json={"reason": "decided at the console"})


def _decided_by_events(store: Any, approval_id: str) -> list[str]:
    events = store.sync_since(0, limit=500)["events"]
    return [event["actor"] for event in events if event["event_type"] == "approval.decided" and event["payload"].get("approval_id") == approval_id]


def test_a_ledger_approved_outside_the_plane_names_its_approver_not_the_caller(ledger_plane: Any) -> None:
    client, store, ledger, orchestrator = ledger_plane
    ledger_id = _pause(ledger_plane)
    assert _outside(client, ledger_id, SECOND_OWNER, "approve", "o1").status_code == 200
    # Another owner then rejects in the app: the ledger's approval stands, and the caller is told so.
    refused = _decide(client, ledger_id, OWNER, "rejected", "r1")
    assert refused.status_code == 409 and "already decided: approved" in refused.text
    mirrored = store.get_approval(f"agt_{ledger_id}", actor="olivia-owner")
    assert (mirrored["status"], mirrored["approved_by"], mirrored["decided_by"]) == ("approved", ["adam-admin"], "adam-admin")
    assert mirrored["decision_reason"] == "decided at the console"
    assert _decided_by_events(store, f"agt_{ledger_id}") == ["adam-admin"]
    # Still mirrored and resumed: the run does not wait for a decision already made.
    assert _stored(ledger, ledger_id)["status"] == "approved" and orchestrator.resumed == [ledger_id]


def test_a_ledger_denied_outside_the_plane_names_who_denied_it(ledger_plane: Any) -> None:
    client, store, _, orchestrator = ledger_plane
    ledger_id = _pause(ledger_plane)
    assert _outside(client, ledger_id, SECOND_OWNER, "deny", "o2").status_code == 200
    refused = _decide(client, ledger_id, OWNER, "approved", "a2")
    assert refused.status_code == 409 and "already decided: rejected" in refused.text
    mirrored = store.get_approval(f"agt_{ledger_id}", actor="olivia-owner")
    assert (mirrored["status"], mirrored["decided_by"], mirrored["approved_by"]) == ("rejected", "adam-admin", [])
    assert orchestrator.resumed == [ledger_id]


def test_the_owner_who_decided_outside_the_plane_gets_their_own_decision(ledger_plane: Any) -> None:
    client, _, _, _ = ledger_plane
    ledger_id = _pause(ledger_plane)
    assert _outside(client, ledger_id, OWNER, "approve", "o3").status_code == 200
    again = _decide(client, ledger_id, OWNER, "approved", "a3")
    assert again.status_code == 200, again.text
    assert again.json()["approval"]["approved_by"] == ["olivia-owner"]


def test_a_dual_approval_made_outside_the_plane_names_both_approvers(ledger_plane: Any) -> None:
    client, _, _, _ = ledger_plane
    ledger_id = _pause(ledger_plane, dual=True, risk="critical")
    for auth, key in ((OWNER, "d1"), (SECOND_OWNER, "d2")):
        assert client.post(f"/approvals/{ledger_id}/dual-approve", headers={**auth, "idempotency-key": key}, json={}).status_code == 200
    assert _outside(client, ledger_id, SECOND_OWNER, "approve", "o4").status_code == 200
    mirrored = _decide(client, ledger_id, OWNER, "approved", "a4")
    assert mirrored.status_code == 200, mirrored.text
    assert mirrored.json()["approval"]["approved_by"] == ["olivia-owner", "adam-admin"]


@pytest.mark.asyncio
async def test_a_ledger_decision_that_names_nobody_is_mirrored_as_the_ledger(tmp_path: Path) -> None:
    store, ledger, orchestrator, ledger_id = _plane_parts(tmp_path)
    ledger.decide(ledger_id, "approved", {})
    with pytest.raises(ValueError, match="already decided: approved"):
        await _owner_decides(store, ledger, orchestrator, ledger_id)
    mirrored = store.get_approval(f"agt_{ledger_id}", actor="olivia-owner")
    assert (mirrored["approved_by"], mirrored["decided_by"]) == ([LEDGER_ACTOR], LEDGER_ACTOR)
    assert orchestrator.resumed == [ledger_id]


def test_the_mirror_records_the_ledgers_outcome(store: Any) -> None:
    AppSyncApprovalListener(store).on_approval_wait(_event("ledger-1", _proposal(dual=True, risk="critical")))
    mirrored = store.mirror_agent_decision(approval_id="agt_ledger-1", outcome="approved", deciders=["olivia-owner", "adam-admin"], reason="console")
    assert (mirrored["status"], mirrored["approved_by"], mirrored["decided_by"], mirrored["decision_reason"]) == ("approved", ["olivia-owner", "adam-admin"], "adam-admin", "console")
    # A decided approval stays as it is, and only agent approvals mirror a ledger.
    assert store.mirror_agent_decision(approval_id="agt_ledger-1", outcome="rejected", deciders=["oscar"])["status"] == "approved"
    with pytest.raises(ValueError):
        store.mirror_agent_decision(approval_id="appr_desktop", outcome="approved", deciders=["oscar"])
