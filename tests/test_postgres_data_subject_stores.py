"""Data-subject export and erasure in the PostgreSQL twins of the runtime stores.

Security's privacy service (#270) erases a person from every store the runtime
holds. On PostgreSQL deployments those are the stores in
``repositories/postgres_state.py``. Without ``export_subject`` and
``erase_subject``, each one is reported as unsupported and the erasure is
incomplete. These tests hold them to the SQLite stores' rules (#270, #275):
- approvals the person requested go, with their dual-approval rows;
- a finished run goes; a run in flight becomes a cancelled tombstone that cannot
  be resumed or written back; an export never carries a resume token;
- jobs the person sent go, and so do messages to them or queued for them;
- their memory goes; memory governance audit entries stay, without their id;
- everyone else's rows stay.
"""

from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from omnidesk_agent.channels.base import ChannelMessage
from omnidesk_agent.repositories.postgres import PostgresRepositoryFactory
from omnidesk_agent.repositories.postgres_state import ERASED_ACTOR

SUBJECT = "sam-subject"
OTHER = "oscar-other"


@pytest.fixture
def pg() -> Iterator[Any]:
    dsn = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("OMNIDESK_TEST_POSTGRES_DSN is not configured")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = "data_subject_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    conninfo = make_conninfo(dsn, options=f"-csearch_path={schema}")
    factory = PostgresRepositoryFactory(conninfo, pool_size=2)
    try:
        yield SimpleNamespace(factory=factory, conninfo=conninfo)
    finally:
        factory.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _mentions(pg: Any, needle: str) -> list[tuple[str, str]]:
    """(namespace, key) of every runtime-state row whose JSON contains ``needle``."""
    import psycopg

    with psycopg.connect(pg.conninfo) as connection:
        rows = connection.execute(
            "SELECT namespace, key FROM omnidesk_core_state WHERE position(%s in value_json::text) > 0 ORDER BY namespace, key", (needle,)
        ).fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]


def test_approvals_the_subject_requested_go_with_their_dual_approval_rows(pg: Any) -> None:
    dual = pg.factory.dual_approval_store()
    approvals = pg.factory.approval_store(ttl_seconds=600, dual_approval_store=dual)
    critical = approvals.create({"tool": "files", "action": "delete", "created_by": SUBJECT, "requires_dual_approval": True, "args": {"path": "sam/clinic.pdf"}})
    plain = approvals.create({"tool": "files", "action": "read", "created_by": SUBJECT})
    theirs = approvals.create({"tool": "files", "action": "read", "created_by": OTHER, "requires_dual_approval": True})
    dual.approve(theirs, SUBJECT)

    assert {row["id"] for row in approvals.export_subject(SUBJECT)} == {critical, plain}
    assert approvals.erase_subject(SUBJECT) == 2

    assert approvals.get(critical) is None and approvals.get(plain) is None
    with pytest.raises(KeyError):
        dual.status(critical)
    # As in security's SQLite store, someone else's request keeps the record of
    # who approved it; that is the only row still naming the subject.
    assert approvals.get(theirs) is not None and dual.status(theirs).first_approver == SUBJECT
    assert _mentions(pg, SUBJECT) == [("dual_approvals", theirs)]
    assert approvals.erase_subject(SUBJECT) == 0


def test_a_finished_run_goes_and_one_in_flight_becomes_a_tombstone_nobody_can_write_back(pg: Any) -> None:
    runs = pg.factory.run_store()
    finished = runs.create({"channel": "telegram", "sender_id": SUBJECT, "text": "summarise my clinic letter"})
    runs.complete(finished, "completed", [{"ok": True, "output": "clinic letter summary"}])
    waiting = runs.create({"channel": "telegram", "sender_id": SUBJECT, "text": "send the clinic letter"})
    token = runs.save_waiting(waiting, {"steps": [{"tool": "email"}]}, 0, [], "appr-clinic", {"tool": "email", "args": {"to": "clinic"}})
    other = runs.create({"channel": "telegram", "sender_id": OTHER, "text": "weekly report"})

    exported = runs.export_subject(SUBJECT)
    assert {run["id"] for run in exported} == {finished, waiting}
    assert all("resume_token" not in run for run in exported)
    assert runs.erase_subject(SUBJECT) == 2

    assert runs.get(finished) is None
    tombstone = runs.get(waiting)
    assert tombstone is not None
    assert tombstone["status"] == "cancelled" and tombstone["original_message"] == {"erased": True}
    assert tombstone["plan_json"] is None and tombstone["results"] == [] and tombstone["approval_proposal"] is None
    assert tombstone["resume_token"] is None and tombstone["waiting_approval_id"] is None and tombstone["erased_at"]
    # Nobody can resume it, and a loop that is still running cannot write it back.
    with pytest.raises(PermissionError):
        runs.consume_resume_token(waiting, token)
    runs.complete(waiting, "completed", [{"ok": True, "output": "clinic letter sent"}])
    assert runs.get(waiting) == tombstone
    assert runs.get_by_approval("appr-clinic") is None
    assert runs.get(other)["original_message"]["sender_id"] == OTHER
    assert _mentions(pg, SUBJECT) == [] and _mentions(pg, "clinic") == []
    assert runs.erase_subject(SUBJECT) == 0


def test_jobs_the_subject_sent_and_messages_for_them_go(pg: Any) -> None:
    jobs = pg.factory.job_queue()
    done = jobs.enqueue(ChannelMessage(channel="telegram", sender_id=SUBJECT, text="book the clinic", message_id="m1"))["job_id"]
    jobs.complete(done, {"reply": "clinic booked"})
    queued = jobs.enqueue(ChannelMessage(channel="telegram", sender_id=SUBJECT, text="move the clinic visit", message_id="m2"))["job_id"]
    theirs = jobs.enqueue(ChannelMessage(channel="telegram", sender_id=OTHER, text="weekly report", message_id="m3"))["job_id"]
    outbound = pg.factory.outbound_messages()
    to_subject = outbound.create(channel="telegram", recipient=SUBJECT, payload={"text": "your clinic booking"})
    for_subject = outbound.create(channel="email", recipient="desk@example.test", payload={"text": "clinic booking request", "requested_by": SUBJECT})
    to_other = outbound.create(channel="telegram", recipient=OTHER, payload={"text": "report"})

    exported_jobs = {job["id"]: job for job in jobs.export_subject(SUBJECT)}
    assert set(exported_jobs) == {done, queued}
    assert exported_jobs[done]["message"]["text"] == "book the clinic" and exported_jobs[done]["result"] == {"reply": "clinic booked"}
    assert {message["id"] for message in outbound.export_subject(SUBJECT)} == {to_subject, for_subject}
    assert jobs.erase_subject(SUBJECT) == 2
    assert outbound.erase_subject(SUBJECT) == 2

    assert jobs.get(done) is None and jobs.get(queued) is None and jobs.get(theirs) is not None
    # The subject's queued task never runs; the next claim is someone else's.
    claimed = jobs.claim_next()
    assert claimed is not None and claimed["id"] == theirs
    assert outbound.get(to_subject) is None and outbound.get(for_subject) is None and outbound.get(to_other) is not None
    assert _mentions(pg, SUBJECT) == [] and _mentions(pg, "clinic") == []


def test_memory_goes_and_governance_entries_stay_without_the_subject(pg: Any) -> None:
    memory = pg.factory.memory_store()
    memory.add("Sam asked about the clinic", plan="look it up", outcome="answered", channel="telegram", actor=SUBJECT)
    memory.add_experience({"task_type": "booking", "goal": "book the clinic visit", "success": True}, channel="telegram", actor=SUBJECT)
    memory.add("weekly report", plan="collect", outcome="sent", channel="telegram", actor=OTHER)
    audit_before = len(memory.state.values(memory.namespace_audit))

    exported = memory.export_subject(SUBJECT)
    assert {record["kind"] for record in exported} == {"experience", "structured_experience", "memory_governance_audit"}
    assert "clinic" in json.dumps(exported)  # decrypted

    assert memory.erase_subject(SUBJECT) >= 3
    assert memory.export_subject(SUBJECT) == []
    audit = memory.state.values(memory.namespace_audit)
    # Every governance decision is still on record; none names the subject.
    assert len(audit) == audit_before
    assert any(event.get("namespace") == f"telegram:{ERASED_ACTOR}" for event in audit)
    assert any(event.get("namespace") == f"telegram:{OTHER}" for event in audit)
    assert _mentions(pg, SUBJECT) == [] and _mentions(pg, "clinic") == []
    assert [row["actor"] for row in memory.state.values(memory.namespace_legacy)] == [OTHER]
