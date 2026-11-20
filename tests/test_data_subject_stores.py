"""Data-subject access and erasure in agent-core's SQLite stores.

privacy.data_subject.DataSubjectService calls export_subject(actor) and
erase_subject(actor) on the memory, run, outbound-message and job stores.
These tests hold each store to what the person is owed. The export holds every
record about them and nobody else's. After an erasure, nothing about them is
left to read, search or recover from the database files. Other people's
records, and the stores' own guarantees (no send, no resume, no rewrite), stay
intact.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from omnidesk_agent.config import MemoryPrivacyConfig, MemoryRetrievalConfig
from omnidesk_agent.core.job_queue import JobQueue
from omnidesk_agent.core.models import ApprovalDecision, ChannelMessage
from omnidesk_agent.core.outbound_messages import OutboundMessageStore
from omnidesk_agent.core.run_store import RunStore
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.channel_send import ChannelSendTool

ALICE, BOB = "alice-42", "bob-7"
SECRET = "zebracornquill"  # a word only alice's records contain
BOB_TEXT = "weekly report for the warehouse"


def file_bytes(db: Path) -> bytes:
    """The database file with its write-ahead log and journal: what someone with disk access can read."""
    return b"".join(path.read_bytes() for path in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-journal")) if path.exists())


def experience(goal: str) -> dict:
    return {"task_type": "general", "goal": goal, "success": True, "risk_level": "low", "memory_status": "validated", "recommended_next_action": f"repeat: {goal}", "raw_trace": {"note": goal}, "tags": ["t"]}


class WordEmbedder:
    model_name = "test-words"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text) % 7), float(text.count(" ")), 1.0] for text in texts]


def fill_memory(store: ExperienceStore) -> None:
    store.add(f"book a table, {SECRET}", plan="call the restaurant", outcome="booked", channel="telegram", actor=ALICE)
    store.add_experience(experience(f"order flowers {SECRET}"), channel="telegram", actor=ALICE)
    # The same person id on another channel is the same data subject.
    store.add_experience(experience(f"pay the rent {SECRET}"), channel="slack", actor=ALICE)
    store.record_interaction_profile(channel="telegram", actor=ALICE, task=f"order flowers {SECRET}", status="completed")
    store.add(BOB_TEXT, channel="telegram", actor=BOB)
    store.add_experience(experience(BOB_TEXT), channel="telegram", actor=BOB)


# --------------------------------------------------------------------------- memory


@pytest.mark.parametrize("encrypted", [False, True])
def test_memory_export_holds_every_record_about_the_person_and_no_one_elses(tmp_path, monkeypatch, encrypted):
    monkeypatch.setenv("OMNIDESK_MEMORY_ENCRYPTION_KEY", "unit-test-memory-key")
    config = MemoryPrivacyConfig(encrypt_at_rest=encrypted, encryption_key_env="OMNIDESK_MEMORY_ENCRYPTION_KEY")
    with ExperienceStore(tmp_path / "memory.sqlite3", config) as store:
        fill_memory(store)
        records = store.export_subject(ALICE)

    text = json.dumps(records)  # JSON-safe as the service returns it
    assert BOB_TEXT not in text and BOB not in text
    assert sorted({r["kind"] for r in records}) == ["experience", "interaction_profile", "memory_governance_audit", "structured_experience"]
    # Readable even when the store encrypts at rest, and from both channels.
    assert next(r for r in records if r["kind"] == "experience")["task"] == f"book a table, {SECRET}"
    structured = [r for r in records if r["kind"] == "structured_experience"]
    assert {r["channel"] for r in structured} == {"telegram", "slack"} and all(SECRET in r["goal"] for r in structured)
    assert next(r for r in records if r["kind"] == "interaction_profile")["last_task"] == f"order flowers {SECRET}"


def test_memory_erasure_leaves_nothing_to_read_search_or_recover(tmp_path):
    db = tmp_path / "memory.sqlite3"
    with ExperienceStore(db) as store:
        store.configure_retrieval(MemoryRetrievalConfig(embeddings_enabled=True), embedder=WordEmbedder())
        fill_memory(store)
        # Retrieval embeds the memories it ranks: alice's get vectors too.
        assert store.retrieve_for_task("order flowers", limit=4, channel="telegram", actor=ALICE)
        assert store.retrieve_for_task("weekly report", limit=4, channel="telegram", actor=BOB)
        alice_ids = {row[0] for row in store.conn.execute("SELECT id FROM structured_experiences WHERE actor=?", (ALICE,))}
        assert alice_ids & {row[0] for row in store.conn.execute("SELECT experience_id FROM structured_experience_embeddings")}
        exported = store.export_subject(ALICE)

        erased = store.erase_subject(ALICE)

        assert erased == len(exported)  # every exported record was deleted or anonymised
        assert store.export_subject(ALICE) == []
        # The full-text indexes hold external content: without a rebuild the words would stay searchable.
        for table in ("experiences_fts", "structured_experiences_fts"):
            assert store.conn.execute(f"SELECT rowid FROM {table} WHERE {table} MATCH ?", (SECRET,)).fetchall() == []  # nosec B608
        assert not alice_ids & {row[0] for row in store.conn.execute("SELECT experience_id FROM structured_experience_embeddings")}
        # The memory governance audit keeps each decision, without the person.
        events = [json.loads(row[0]) for row in store.conn.execute("SELECT event_json FROM memory_governance_audit")]
        assert any(e.get("namespace") == "telegram:[erased]" for e in events) and not any(ALICE in json.dumps(e) for e in events)
        # Other people's memory still works.
        assert store.retrieve_for_task("weekly report", limit=4, channel="telegram", actor=BOB)
        assert store.search("warehouse", limit=5)

    assert SECRET.encode() not in file_bytes(db) and ALICE.encode() not in file_bytes(db)


# --------------------------------------------------------------------------- runs


def message(sender: str, text: str, channel: str = "telegram") -> dict:
    return asdict(ChannelMessage(channel=channel, sender_id=sender, text=text))


def test_run_export_has_the_persons_runs_without_their_resume_tokens(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    done = store.create(message(ALICE, f"plan my trip {SECRET}"))
    store.complete(done, "completed", [{"ok": True, "summary": f"booked {SECRET}"}])
    waiting = store.create(message(ALICE, f"send the invoice {SECRET}", channel="slack"))
    token = store.save_waiting(waiting, {"goal": SECRET}, 0, [], "approval-1", {"tool": "channels", "args": {"text": SECRET}})
    store.create(message(BOB, BOB_TEXT))

    exported = store.export_subject(ALICE)

    assert sorted(run["id"] for run in exported) == sorted([done, waiting])
    assert next(run for run in exported if run["id"] == done)["results"][0]["summary"] == f"booked {SECRET}"
    # A resume token is a credential for the run, not data about the person.
    assert all("resume_token" not in run for run in exported) and token not in json.dumps(exported)
    assert BOB_TEXT not in json.dumps(exported)


def test_run_erasure_deletes_finished_runs_and_stops_one_in_flight(tmp_path):
    db = tmp_path / "runs.sqlite3"
    store = RunStore(db)
    done = store.create(message(ALICE, f"plan my trip {SECRET}"))
    store.complete(done, "completed", [{"ok": True, "summary": f"booked {SECRET}"}])
    waiting = store.create(message(ALICE, f"send the invoice {SECRET}"))
    token = store.save_waiting(waiting, {"goal": SECRET}, 0, [{"summary": SECRET}], "approval-1", {"tool": "channels", "args": {"text": SECRET}})
    running = store.create(message(ALICE, f"tidy the inbox {SECRET}"))
    store.update(running, {"status": "running"})
    bobs = store.create(message(BOB, BOB_TEXT))

    assert store.erase_subject(ALICE) == 3

    assert store.get(done) is None
    assert store.get(bobs)["erased"] is False
    for run_id in (waiting, running):
        tomb = store.get(run_id)
        assert tomb is not None and tomb["status"] == "cancelled" and tomb["original_message"] == {"erased": True} and tomb["erased"] is True
        assert (tomb["plan_json"], tomb["results"], tomb["approval_proposal"], tomb["waiting_approval_id"]) == (None, [], None, None)
    # Nobody can resume the erased run.
    with pytest.raises(PermissionError):
        store.require_resume_token(waiting, token)
    with pytest.raises(PermissionError):
        store.consume_resume_token(waiting, token)
    # A loop still running for it cannot write the content back.
    store.update(running, {"plan_json": json.dumps({"goal": SECRET}), "results_json": json.dumps([{"summary": SECRET}])})
    store.complete(running, "completed", [{"summary": SECRET}])
    assert store.get(running)["status"] == "cancelled" and SECRET not in json.dumps(store.get(running))
    assert store.export_subject(ALICE) == [] and store.get(bobs)["original_message"]["text"] == BOB_TEXT
    assert SECRET.encode() not in file_bytes(db)


# --------------------------------------------------------------------------- outbound messages


class Allow:
    def verify(self, proposal):
        return ApprovalDecision(True, "allow", "test")


class Adapter:
    async def send_text(self, recipient, text, **kwargs):
        return {"message_id": "m1"}


def test_outbound_messages_to_or_for_the_person_are_exported_and_never_sent_after_erasure(tmp_path):
    db = tmp_path / "outbound.sqlite3"
    store = OutboundMessageStore(db)
    tool = ChannelSendTool({"telegram": Adapter()}, store)
    # Queued by alice's run for a third party: found through the requester.
    queued = asyncio.run(tool.call("send_text", {"channel": "telegram", "recipient": "landlord-9", "text": f"rent for {SECRET}"}, ToolContext(permissions=Allow(), actor=ALICE)))
    assert queued.ok
    # Addressed to alice, already delivered.
    to_alice = store.create(channel="telegram", recipient=ALICE, payload={"type": "text", "text": f"hello {SECRET}", "requested_by": "owner"})
    store.mark_sent(to_alice, provider_message_id="p1")
    bobs = store.create(channel="telegram", recipient=BOB, payload={"type": "text", "text": BOB_TEXT, "requested_by": BOB})

    exported = store.export_subject(ALICE)

    assert sorted((r["recipient"], r["status"]) for r in exported) == [(ALICE, "sent"), ("landlord-9", "pending")]
    assert next(r for r in exported if r["recipient"] == "landlord-9")["payload"]["requested_by"] == ALICE
    assert BOB_TEXT not in json.dumps(exported)

    assert store.erase_subject(ALICE) == 2

    assert store.export_subject(ALICE) == []
    # The message queued for alice's landlord is never sent; bob's still is.
    claimed = store.claim_next()
    assert claimed is not None and claimed["id"] == bobs and store.claim_next() is None
    assert SECRET.encode() not in file_bytes(db)


# --------------------------------------------------------------------------- jobs


def test_task_messages_the_person_sent_are_exported_and_erased_from_the_job_queue(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    queue = JobQueue(db)
    first = queue.enqueue(ChannelMessage(channel="telegram", sender_id=ALICE, text=f"translate {SECRET}", message_id="m1"))
    claimed = queue.claim_next()
    assert claimed is not None and claimed["id"] == first["job_id"]
    queue.complete(first["job_id"], {"final_answer": f"translated {SECRET}"})
    queue.enqueue(ChannelMessage(channel="telegram", sender_id=ALICE, text=f"remind me about {SECRET}", message_id="m2"))
    bobs = queue.enqueue(ChannelMessage(channel="telegram", sender_id=BOB, text=BOB_TEXT, message_id="m3"))

    exported = queue.export_subject(ALICE)

    assert sorted(r["message"]["text"] for r in exported) == [f"remind me about {SECRET}", f"translate {SECRET}"]
    assert next(r for r in exported if r["status"] == "completed")["result"] == {"final_answer": f"translated {SECRET}"}
    assert BOB_TEXT not in json.dumps(exported)

    assert queue.erase_subject(ALICE) == 2

    assert queue.export_subject(ALICE) == []
    # The queued task alice sent never runs; bob's does.
    claimed = queue.claim_next()
    assert claimed is not None and claimed["id"] == bobs["job_id"] and queue.claim_next() is None
    assert SECRET.encode() not in file_bytes(db)
