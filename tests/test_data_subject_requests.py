"""Data-subject export and erasure: complete about what they cover, dual-approved to erase.

The 2026-11-21 audit critic: there was no way to export or erase one user's
data; the only deletion was memory's purge_expired.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from omnidesk_agent.config import AppConfig
from omnidesk_agent.privacy.data_subject import AuditLogSubjectSource, DataSubjectService

TOKENS = {"operator": "p" * 40, "owner": "o" * 40, "admin": "a" * 40}


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
    store = application.state.runtime.approval_store
    for actor, n in (("alice", 2), ("bob", 1)):
        for i in range(n):
            store.create({"tool": "files", "action": "write_text", "risk": "high", "actor": actor, "created_by": actor, "args": {"text": f"{actor}-{i}"}})
    application.state.runtime.permissions.verify({"tool": "files", "action": "list", "risk": "low", "source": "local-cli", "actor": "alice"})
    return application


def _h(role: str, key: str | None = None) -> dict[str, str]:
    headers = {"authorization": f"Bearer {TOKENS[role]}"}
    if key:
        headers["idempotency-key"] = key
    return headers


def test_export_covers_supported_stores_and_names_the_rest(app):
    with TestClient(app) as client:
        export = client.get("/admin/privacy/subjects/alice/export", headers=_h("owner")).json()

    assert sorted(r["proposal"]["args"]["text"] for r in export["data"]["approvals"]) == ["alice-0", "alice-1"]
    assert export["data"]["security_audit_log"] and all(
        e["payload"].get("actor") == "alice" for e in export["data"]["security_audit_log"]
    )
    assert "security_audit_log" in export["retained"]
    # Every store the runtime has is exported or named unsupported, and
    # "complete" follows from that; this holds before and after stores gain hooks.
    stores = {"approvals": "approval_store", "memory": "memory", "runs": "run_store", "outbound_messages": "outbound_messages", "app_sync": "app_sync", "jobs": "job_queue"}
    present = {name for name, attr in stores.items() if getattr(app.state.runtime, attr, None) is not None}
    assert "jobs" in present  # the job queue keeps every inbound message
    assert (set(export["data"]) - set(export["retained"])) | set(export["unsupported"]) == present
    assert export["complete"] is (not export["unsupported"])


def test_export_needs_an_owner(app):
    with TestClient(app) as client:
        assert client.get("/admin/privacy/subjects/alice/export", headers=_h("operator")).status_code == 403


def test_erasure_needs_two_other_owners_and_erases_only_that_subject(app):
    rt = app.state.runtime
    with TestClient(app) as client:
        requested = client.post("/admin/privacy/subjects/alice/erase-requests", headers=_h("operator", "req-1")).json()
        approval_id = requested["approval_id"]
        assert requested["requires_dual_approval"] is True

        # Not yet approved: nothing happens.
        early = client.post(f"/admin/privacy/subjects/alice/erase/{approval_id}", headers=_h("owner", "exec-0"))
        assert early.status_code == 403
        assert len(rt.approval_store.export_subject("alice")) == 2

        assert client.post(f"/approvals/{approval_id}/dual-approve", headers=_h("owner", "dual-1"), json={}).status_code == 200
        assert client.post(f"/approvals/{approval_id}/dual-approve", headers=_h("admin", "dual-2"), json={}).status_code == 200
        assert client.post(f"/approvals/{approval_id}/approve", headers=_h("owner", "approve-1"), json={}).status_code == 200

        # The approval is bound to alice: it cannot erase bob.
        assert client.post(f"/admin/privacy/subjects/bob/erase/{approval_id}", headers=_h("owner", "exec-bob")).status_code == 403

        done = client.post(f"/admin/privacy/subjects/alice/erase/{approval_id}", headers=_h("owner", "exec-1")).json()
        replay = client.post(f"/admin/privacy/subjects/alice/erase/{approval_id}", headers=_h("owner", "exec-1")).json()

    assert done["erased"]["approvals"] == 2 and done == replay
    assert rt.approval_store.export_subject("alice") == []
    assert len(rt.approval_store.export_subject("bob")) == 1
    events = [json.loads(line) for line in rt.permissions.audit_log.read_text().splitlines()]
    erased = [e for e in events if e.get("event") == "privacy.subject_erased"]
    assert erased and erased[0]["payload"]["erased"]["approvals"] == 2 and "alice-0" not in json.dumps(erased)


def test_the_requester_cannot_approve_their_own_erasure(app):
    with TestClient(app) as client:
        approval_id = client.post("/admin/privacy/subjects/alice/erase-requests", headers=_h("owner", "req-2")).json()["approval_id"]
        refused = client.post(f"/approvals/{approval_id}/dual-approve", headers=_h("owner", "self-1"), json={})
        assert refused.status_code == 409 and "creator cannot approve" in refused.json()["detail"]


def test_service_reports_unsupported_stores_and_audits_counts_only(tmp_path):
    class Store:
        def __init__(self):
            self.rows = {"alice": [{"x": "secret"}]}

        def export_subject(self, actor):
            return list(self.rows.get(actor, []))

        def erase_subject(self, actor):
            return len(self.rows.pop(actor, []))

    events = []
    log = tmp_path / "audit.log"
    log.write_text(json.dumps({"event": "proposal", "payload": {"actor": "alice"}}) + "\n" + json.dumps({"event": "proposal", "payload": {"actor": "bob"}}) + "\n")
    service = DataSubjectService({"store": Store(), "legacy": object()}, retained={"audit": (AuditLogSubjectSource(log), "kept")}, audit=lambda e, p: events.append((e, p)))

    exported = service.export("alice", requested_by="owner-a")
    erased = service.erase("alice", requested_by="owner-a", approval_id="ap-1")

    assert exported["data"]["store"] == [{"x": "secret"}] and len(exported["data"]["audit"]) == 1
    assert exported["unsupported"] == ["legacy"] and erased["erased"] == {"store": 1} and erased["complete"] is False
    assert all("secret" not in json.dumps(payload) for _event, payload in events)
    with pytest.raises(ValueError):
        service.export(" ", requested_by="owner-a")
