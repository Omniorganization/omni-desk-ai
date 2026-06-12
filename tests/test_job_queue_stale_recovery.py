from __future__ import annotations

from pathlib import Path

from omnidesk_agent.core.job_queue import JobQueue
from omnidesk_agent.core.models import ChannelMessage


def _running_job(queue: JobQueue, tmp_path: Path) -> str:
    msg = ChannelMessage(channel="telegram", sender_id="u1", thread_id="c1", message_id="m1", text="hello")
    job_id = queue.enqueue(msg)["job_id"]
    assert queue.claim_next()["id"] == job_id
    return job_id


def test_running_job_recovered_after_worker_crash(tmp_path: Path):
    queue = JobQueue(tmp_path / "jobs.sqlite3", max_retries=3, base_retry_seconds=0)
    job_id = _running_job(queue, tmp_path)

    recovered = queue.recover_stale_running(lease_seconds=0)

    job = queue.get(job_id)
    assert recovered == 1
    assert job["status"] == "retry"
    assert job["retry_count"] == 1
    assert job["locked_at"] is None


def test_stale_running_job_retry_count_increments_to_dead_letter(tmp_path: Path):
    queue = JobQueue(tmp_path / "jobs.sqlite3", max_retries=0, base_retry_seconds=0)
    job_id = _running_job(queue, tmp_path)

    assert queue.recover_stale_running(lease_seconds=0) == 1

    job = queue.get(job_id)
    assert job["status"] == "dead_letter"
    assert job["retry_count"] == 1


def test_non_stale_running_job_not_recovered(tmp_path: Path):
    queue = JobQueue(tmp_path / "jobs.sqlite3", max_retries=3, base_retry_seconds=0)
    job_id = _running_job(queue, tmp_path)

    assert queue.recover_stale_running(lease_seconds=3600) == 0
    assert queue.get(job_id)["status"] == "running"
