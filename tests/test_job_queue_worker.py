from __future__ import annotations

import asyncio
from pathlib import Path

from omnidesk_agent.core.job_queue import JobQueue
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.core.worker import WebhookWorker


class DummyOrchestrator:
    def __init__(self):
        self.messages = []

    async def handle_message(self, message):
        self.messages.append(message)
        return {"ok": True, "echo": message.text}


class FailingOrchestrator:
    async def handle_message(self, message):
        raise RuntimeError("boom")


def test_job_queue_is_idempotent_by_channel_source_and_message_id(tmp_path: Path):
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    msg = ChannelMessage(channel="telegram", sender_id="u1", thread_id="c1", message_id="m1", text="hello")

    first = queue.enqueue(msg)
    second = queue.enqueue(msg)

    assert first["job_id"] == second["job_id"]
    assert first["created"] is True
    assert second["created"] is False
    assert queue.stats() == {"pending": 1}


def test_webhook_worker_completes_claimed_job(tmp_path: Path):
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    orchestrator = DummyOrchestrator()
    worker = WebhookWorker(queue, orchestrator)
    msg = ChannelMessage(channel="telegram", sender_id="u1", thread_id="c1", message_id="m1", text="hello")
    job_id = queue.enqueue(msg)["job_id"]

    assert asyncio.run(worker.run_once()) is True

    job = queue.get(job_id)
    assert job is not None
    assert job["status"] == "completed"
    assert orchestrator.messages[0].text == "hello"


def test_webhook_worker_retries_then_dead_letters(tmp_path: Path):
    queue = JobQueue(tmp_path / "jobs.sqlite3", max_retries=0, base_retry_seconds=0)
    worker = WebhookWorker(queue, FailingOrchestrator())
    msg = ChannelMessage(channel="telegram", sender_id="u1", thread_id="c1", message_id="m1", text="hello")
    job_id = queue.enqueue(msg)["job_id"]

    assert asyncio.run(worker.run_once()) is True

    job = queue.get(job_id)
    assert job is not None
    assert job["status"] == "dead_letter"
    assert "boom" in (job["last_error"] or "")
