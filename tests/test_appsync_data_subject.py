"""Data-subject export and erasure in the AppSync stores (JSON and PostgreSQL).

Security's privacy service (#270) asks every store the runtime holds for
``export_subject(actor)`` and ``erase_subject(actor)``; a store without them is
reported as unsupported, and the erasure is incomplete. AppSync holds most of
what the clients write about a person: chats, tasks, approvals, devices,
pushes, offline-sync state. These tests hold both backends to the rules in
``appsync/subject_data.py``:
- an export has the person's records and nobody else's;
- after an erasure the person's id and content are nowhere in the store, while
  everyone else's records stay;
- an erased device stays revoked and gets no pushes;
- an erased approver never counts toward four eyes, and a pairing code the
  person issued can no longer enrol a device;
- approvals follow security's one rule for every store (#270): kept without
  the person, content withheld, and withdrawn when still pending.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from omnidesk_agent.appsync.agent_approvals import AppSyncApprovalListener
from omnidesk_agent.appsync.projects import GatewayProjectStore
from omnidesk_agent.appsync.strict_json_store import StrictJsonAppSyncStore
from omnidesk_agent.appsync.subject_data import ERASED_ACTOR, anonymized_proposal

SUBJECT = "sam-subject"
OTHER = "oscar-operator"
OWNER = "olivia-owner"
ADMIN = "adam-admin"
PHONE = "dev_sam_phone"
DESKTOP = "dev_sam_desktop"
# Content only the subject wrote or caused; none of it may survive an erasure.
SUBJECT_TEXT = ("Sam's clinic appointment", "Reply about the clinic", "Print the clinic letter", "sams-macbook.local", "fcm-sam-token-0001", "Sam's iPhone")
OTHER_TASK = "Rotate the shared build key"


class Key:
    """A P-256 device key that signs like the clients do (raw r||s, base64)."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public_pem = self.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def sign(self, message: str) -> str:
        r, s = decode_dss_signature(self.key.sign(message.encode(), ec.ECDSA(hashes.SHA256())))
        return "base64:" + base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()


@pytest.fixture(params=["json", "postgres"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    # The local outbox queues every event as an offline operation, so the
    # erasure must also find the person in the offline-sync tables.
    if request.param == "json":
        yield StrictJsonAppSyncStore(tmp_path / "appsync.json", local_outbox_enabled=True)
        return
    dsn = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("OMNIDESK_TEST_POSTGRES_DSN is not configured")
    from omnidesk_agent.appsync.migrated_postgres_store import MigratedMultiInstancePostgresAppSyncStore
    from omnidesk_agent.appsync.postgres_migrations import apply_appsync_migrations

    namespace = f"subject_{uuid.uuid4().hex}"
    apply_appsync_migrations(dsn, namespace=namespace)
    pg = MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=namespace, pool_size=2, local_outbox_enabled=True)
    try:
        yield pg
    finally:
        pg.close()


def _mentions(store: Any, needle: str) -> list[str]:
    """Every stored row of this store that contains ``needle``: on JSON, every line
    of its state file and of the files beside it (the project store and its backups)."""
    if hasattr(store, "path"):
        state = Path(store.path)
        return [
            f"{path.name}: {line.strip()}"
            for path in sorted(state.parent.glob(f"{state.name}*"))
            if path.is_file()
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if needle in line
        ]
    import psycopg
    from psycopg import sql

    found: list[str] = []
    with psycopg.connect(store.dsn) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT table_name FROM information_schema.columns WHERE column_name = 'namespace' AND table_schema = current_schema() AND table_name LIKE 'omnidesk_appsync_%' ORDER BY table_name"
        ).fetchall()]
        for table in tables:
            query = sql.SQL("SELECT t::text FROM {} t WHERE t.namespace = %s AND position(%s in t::text) > 0").format(sql.Identifier(table))
            found += [f"{table}: {row[0][:300]}" for row in conn.execute(query, (store.namespace, needle)).fetchall()]
    return found


def _verified_phone(store: Any) -> None:
    key = Key()
    store.register_device(actor=SUBJECT, device_id=PHONE, device_type="mobile", name="Sam's iPhone", platform="ios", public_key=key.public_pem)
    enrollment = store.start_device_enrollment(actor=SUBJECT, device_type="mobile", pairing_code="pairing-code-sam-1")
    store.complete_device_enrollment(actor=SUBJECT, enrollment_id=enrollment["enrollment_id"], pairing_code="pairing-code-sam-1", device_id=PHONE, public_key=key.public_pem)
    challenge = store.issue_device_challenge(actor=SUBJECT, enrollment_id=enrollment["enrollment_id"], device_id=PHONE)
    store.verify_device_challenge(actor=SUBJECT, enrollment_id=enrollment["enrollment_id"], challenge_id=challenge["challenge_id"], device_id=PHONE, signature=key.sign(challenge["signing_message"]))
    store.register_push_token(actor=SUBJECT, device_id=PHONE, push_token="fcm-sam-token-0001", platform="ios")


def _world(store: Any) -> dict[str, Any]:
    """The subject's chat, gated task, devices and sync state, next to someone else's request they approved."""
    for actor, role in ((SUBJECT, "operator"), (OTHER, "operator"), (OWNER, "owner"), (ADMIN, "admin")):
        store.ensure_user(actor, role=role)
    conversation = store.create_conversation(actor=SUBJECT, title=SUBJECT_TEXT[0], idempotency_key="conv-1", idempotency_payload={"title": SUBJECT_TEXT[0]})
    conversation_id = conversation["conversation_id"]
    store.add_chat_user_message(actor=SUBJECT, conversation_id=conversation_id, content=SUBJECT_TEXT[0])
    store.add_assistant_message(actor=SUBJECT, conversation_id=conversation_id, content=SUBJECT_TEXT[1], provider="test", model="test-model", profile="default")
    mine = store.add_message_and_task(actor=SUBJECT, conversation_id=conversation_id, content=SUBJECT_TEXT[2], requires_desktop_runtime=True, risk="high")
    _verified_phone(store)
    store.register_device(actor=SUBJECT, device_id=DESKTOP, device_type="desktop", name="Sam's MacBook", platform="macos")
    store.heartbeat_runtime(actor=SUBJECT, device_id=DESKTOP, status="online", hostname=SUBJECT_TEXT[3])
    store.record_sync_cursor(actor=SUBJECT, remote="primary", since_seq=3, device_id=PHONE)
    # A project the subject owns, changed twice so the local store also keeps backups naming them.
    projects = GatewayProjectStore(store)
    project = projects.create_project(actor=SUBJECT, name="Clinic paperwork", description="forms")
    projects.update_project(actor=SUBJECT, project_id=project["project_id"], patch={"description": "forms and letters"})
    other_conversation = store.create_conversation(actor=OTHER, title="Shared build")
    other = store.add_message_and_task(actor=OTHER, conversation_id=other_conversation["conversation_id"], content=OTHER_TASK, requires_desktop_runtime=True, risk="high")
    store.decide_approval(approval_id=other["approval"]["approval_id"], actor=SUBJECT, decision="approved", reason=f"fine by {SUBJECT}")
    return {"conversation_id": conversation_id, "task_id": mine["task"]["task_id"], "approval_id": mine["approval"]["approval_id"], "other": other, "other_conversation_id": other_conversation["conversation_id"], "project_id": project["project_id"]}


def _kinds(records: list[dict[str, Any]]) -> set[str]:
    return {str(record["record"]) for record in records}


def test_an_export_has_the_subjects_records_and_nobody_elses(store: Any) -> None:
    world = _world(store)
    records = store.export_subject(SUBJECT)
    assert {"user", "device", "runtime", "conversation", "message", "task", "approval", "approval_decision", "notification", "push",
            "device_enrollment", "local_outbox", "sync_cursor", "operation_log", "sync_event", "idempotency", "project"} <= _kinds(records)
    assert [record["project_id"] for record in records if record["record"] == "project"] == [world["project_id"]]
    text = json.dumps(records)  # JSON-safe, as the privacy service returns it
    for content in SUBJECT_TEXT[:4]:
        assert content in text
    # Someone else's request appears only as the subject's decision on it.
    assert OTHER_TASK not in text
    decision = next(record for record in records if record["record"] == "approval_decision")
    assert decision == {**decision, "approval_id": world["other"]["approval"]["approval_id"], "decided": True, "approved": True}
    # Never a credential: no push target, device token hash or pairing code hash.
    for field in ("push_token", "token_hash", "pairing_code_hash", "last_challenge_nonce_hash"):
        assert all(field not in record for record in records), field
    assert "fcm-sam-token-0001" not in text
    assert all(record.get("actor") in (None, SUBJECT) for record in records if record["record"] in {"conversation", "task", "device", "sync_event"})


def test_after_an_erasure_the_subject_is_nowhere_and_everyone_else_stays(store: Any) -> None:
    world = _world(store)
    other_seqs = [event["seq"] for event in store.sync_since(0, actor=OTHER, limit=500)["events"]]

    erased = store.erase_subject(SUBJECT)

    assert erased > 0
    for needle in (SUBJECT, *SUBJECT_TEXT):
        assert _mentions(store, needle) == [], needle
    assert _mentions(store, OTHER) and _mentions(store, OTHER_TASK), "the search must see rows that stay"
    assert store.export_subject(SUBJECT) == []
    # Everyone else's records stay. Sync events keep their numbers, so cursors
    # stay valid; one about an erased conversation no longer resolves to a
    # private chat, so others may now see it, reduced to identifiers.
    after = {event["seq"]: event for event in store.sync_since(0, actor=OTHER, limit=500)["events"]}
    assert set(other_seqs) <= set(after)
    assert all(after[seq]["payload"].get("redacted") for seq in set(after) - set(other_seqs))
    assert [message["content"] for message in store.list_messages(world["other_conversation_id"], actor=OTHER)] == [OTHER_TASK]
    kept = store.get_approval(world["other"]["approval"]["approval_id"], actor=OTHER)
    assert kept["status"] == "approved" and kept["decided_by"] == ERASED_ACTOR and kept["approved_by"] == [ERASED_ACTOR]
    assert store.get_task(world["other"]["task"]["task_id"], actor=OTHER)["status"] == "queued"
    # The project stays with the organization, owned by nobody named, and still usable.
    projects = GatewayProjectStore(store)
    [project] = [item for item in projects.list_projects(actor=OTHER) if item["project_id"] == world["project_id"]]
    assert project["owner_actor"] == ERASED_ACTOR and project["description"] == "forms and letters"
    assert projects.update_project(actor=OTHER, project_id=world["project_id"], patch={"archived": True})["archived"] is True
    # Their request stays as the record of a requested action, without them, and is withdrawn.
    withdrawn = store.get_approval(world["approval_id"], actor=OTHER)
    assert (withdrawn["status"], withdrawn["requested_by"], withdrawn["decided_by"], withdrawn["decision_reason"]) == ("rejected", ERASED_ACTOR, ERASED_ACTOR, "requester erased")
    assert withdrawn["action"] == "desktop task (details withheld)"
    # The subject's task is gone: a desktop still holding it cannot write a result back.
    with pytest.raises(KeyError):
        store.update_task_status(task_id=world["task_id"], actor=SUBJECT, status="completed", result_summary="done")
    # A second erasure finds nothing.
    assert store.erase_subject(SUBJECT) == 0


def test_an_erased_device_stays_revoked_and_gets_no_pushes(store: Any) -> None:
    _world(store)
    store.erase_subject(SUBJECT)

    # The same person, still holding a valid token, cannot bring the device back.
    store.ensure_user(SUBJECT, role="operator")
    with pytest.raises(PermissionError):
        store.register_device(actor=SUBJECT, device_id=PHONE, device_type="mobile", name="phone", platform="ios")
    # A new request notifies mobiles; the erased phone is not one of them.
    conversation = store.create_conversation(actor=OTHER, title="later")
    store.add_message_and_task(actor=OTHER, conversation_id=conversation["conversation_id"], content="restart the runner", requires_desktop_runtime=True, risk="high")
    assert store.claim_push_deliveries(worker_id="worker-1") == []


def test_an_erased_approvers_partial_approval_no_longer_counts(store: Any) -> None:
    for actor, role in ((SUBJECT, "owner"), (OTHER, "operator"), (OWNER, "owner"), (ADMIN, "admin")):
        store.ensure_user(actor, role=role)
    conversation = store.create_conversation(actor=OTHER, title="critical")
    created = store.add_message_and_task(actor=OTHER, conversation_id=conversation["conversation_id"], content="delete the production bucket", requires_desktop_runtime=True, risk="critical")
    approval_id = created["approval"]["approval_id"]
    assert store.decide_approval(approval_id=approval_id, actor=SUBJECT, decision="approved")["status"] == "pending"

    store.erase_subject(SUBJECT)

    # Withdrawn rather than anonymised: two identifiable approvers are still needed.
    assert store.get_approval(approval_id, actor=OTHER)["approved_by"] == []
    assert store.decide_approval(approval_id=approval_id, actor=OWNER, decision="approved")["status"] == "pending"
    assert store.decide_approval(approval_id=approval_id, actor=ADMIN, decision="approved")["status"] == "approved"


def test_a_pairing_code_the_subject_issued_cannot_enrol_a_device_after_erasure(store: Any) -> None:
    store.ensure_user(SUBJECT, role="owner")
    enrollment = store.start_device_enrollment(actor=SUBJECT, device_type="desktop", pairing_code="pairing-code-left-open")

    store.erase_subject(SUBJECT)

    store.ensure_user(OWNER, role="owner")
    with pytest.raises(ValueError, match="not pending"):
        store.complete_device_enrollment(actor=OWNER, enrollment_id=enrollment["enrollment_id"], pairing_code="pairing-code-left-open", device_id="dev_intruder", public_key=Key().public_pem)
    assert _mentions(store, SUBJECT) == []


def test_changes_others_queued_about_the_subjects_records_do_not_leave_for_the_remote(store: Any) -> None:
    world = _world(store)
    # An owner rejects the subject's request; the local outbox queues that change for the remote gateway.
    store.decide_approval(approval_id=world["approval_id"], actor=OWNER, decision="rejected", reason="not now")

    def queued_about(*ids: str) -> list[dict[str, Any]]:
        return [op for op in store.pending_local_outbox(limit=500) if any(json.dumps(op["payload"]).count(f'"{item}"') for item in ids)]

    assert queued_about(world["approval_id"])

    store.erase_subject(SUBJECT)

    assert queued_about(world["approval_id"], world["task_id"], world["conversation_id"]) == []
    # Changes about everyone else's records still leave.
    assert queued_about(world["other"]["approval"]["approval_id"])


def test_an_agent_step_the_subject_asked_for_is_withdrawn_and_keeps_no_content(store: Any) -> None:
    for actor, role in ((SUBJECT, "operator"), (OWNER, "owner")):
        store.ensure_user(actor, role=role)
    proposal = {"tool": "files", "action": "write_text", "risk": "high", "created_by": SUBJECT, "actor": SUBJECT, "reason": "Sam asked for the clinic letter",
                "args": {"text": "Dear Dr. Rivera", "text_sha256": "ab" * 32}}
    AppSyncApprovalListener(store).on_approval_wait({
        "run_id": "run_1", "approval_id": "ledger-1", "proposal": proposal, "requester": SUBJECT, "channel": "telegram",
        "risk": "high", "requires_dual_approval": False, "organization": "org_default", "created_at": 1.0, "expires_at": None,
    })

    store.erase_subject(SUBJECT)

    approval = store.get_approval("agt_ledger-1", actor=OWNER)
    assert (approval["status"], approval["requested_by"], approval["decided_by"], approval["decision_reason"]) == ("rejected", ERASED_ACTOR, ERASED_ACTOR, "requester erased")
    # The same rule as the ledger's copy of this request: no requester, no argument content.
    stored = approval["action_detail"]["proposal"]
    assert stored["created_by"] == stored["actor"] == ERASED_ACTOR
    assert stored["args"] == {"text_sha256": "ab" * 32} and stored["args_withheld"] is True
    # Only structure stays, as in the ledger: the planner's reason is dropped and named.
    assert "reason" not in stored and stored["fields_withheld"] == ["reason"]
    assert approval["reason"] == "An agent step waits for approval."
    assert _mentions(store, SUBJECT) == [] and _mentions(store, "Dear Dr. Rivera") == [] and _mentions(store, "clinic") == []
    # Withdrawn means nobody can approve it any more.
    with pytest.raises(ValueError):
        store.decide_approval(approval_id="agt_ledger-1", actor=OWNER, decision="approved")


def test_an_erased_request_keeps_structure_and_no_free_text() -> None:
    # Security's case of the same name (#270): the mirror applies the ledger's rule.
    kept = anonymized_proposal(
        {"tool": "files", "action": "write_text", "risk": "high", "created_by": "sam", "scope_hash": "s" * 64,
         "reason": "Sam asked for the clinic letter", "summary": "letter to Dr. X", "args": {"text": "...", "text_length": 3}},
        "sam",
    )

    assert kept == {
        "tool": "files", "action": "write_text", "risk": "high", "created_by": "[erased]", "scope_hash": "s" * 64,
        "args": {"text_length": 3}, "args_withheld": True, "fields_withheld": ["reason", "summary"],
    }
