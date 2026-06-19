from __future__ import annotations

import pytest

from omnidesk_agent.core.job_queue import JobQueue
from omnidesk_agent.core.models import ChannelMessage


def test_dead_letter_can_be_listed_requeued_and_purged(tmp_path):
    q = JobQueue(tmp_path / "jobs.sqlite3", max_retries=0)
    job_id = q.enqueue(ChannelMessage(channel="telegram", sender_id="u", text="x", message_id="m1"))["job_id"]
    claimed = q.claim_next()
    assert claimed and claimed["id"] == job_id
    failed = q.fail(job_id, "boom")
    assert failed["status"] == "dead_letter"
    assert q.list_dead_letters()[0]["id"] == job_id

    assert q.requeue_dead_letter(job_id) == {"job_id": job_id, "status": "pending"}
    assert q.get(job_id)["status"] == "pending"
    claimed = q.claim_next()
    assert claimed and claimed["id"] == job_id
    q.fail(job_id, "boom again")
    assert q.purge_dead_letter(job_id) == {"job_id": job_id, "purged": True}
    assert q.get(job_id) is None


def test_dead_letter_requeue_rejects_non_dead_letter(tmp_path):
    q = JobQueue(tmp_path / "jobs.sqlite3")
    job_id = q.enqueue(ChannelMessage(channel="telegram", sender_id="u", text="x", message_id="m1"))["job_id"]
    with pytest.raises(ValueError):
        q.requeue_dead_letter(job_id)
