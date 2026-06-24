from __future__ import annotations

import asyncio
from dataclasses import asdict

from omnidesk_agent.core import orchestrator as orchestrator_module
from omnidesk_agent.core import worker as worker_module
from omnidesk_agent.core.job_queue import JobQueue
from omnidesk_agent.core.models import ChannelMessage, Plan
from omnidesk_agent.core.orchestrator import Orchestrator
from omnidesk_agent.core.worker import WebhookWorker


class EmptyPlanner:
    async def plan(self, msg):
        return Plan(goal=msg.text, steps=[], plan_id="plan-async-storage")


class FakeMemory:
    def add(self, **kwargs):
        return None

    def add_experience(self, value):
        return None

    def record_metric(self, **kwargs):
        return None


class FakeRunStore:
    def __init__(self):
        self.completed = []

    def create(self, message):
        assert message["text"] == "hello"
        return "run-1"

    def complete(self, run_id, status, results):
        self.completed.append((run_id, status, list(results)))


def test_orchestrator_delegates_sync_run_store_calls_to_thread(monkeypatch):
    calls: list[str] = []

    async def fake_to_thread(func, /, *args, **kwargs):
        calls.append(getattr(func, "__name__", repr(func)))
        return func(*args, **kwargs)

    monkeypatch.setattr(orchestrator_module.asyncio, "to_thread", fake_to_thread)
    run_store = FakeRunStore()
    orchestrator = Orchestrator(EmptyPlanner(), tools=object(), permissions=object(), memory=FakeMemory(), run_store=run_store)

    result = asyncio.run(orchestrator.handle_message(ChannelMessage(channel="test", sender_id="u", text="hello")))

    assert result["status"] == "completed"
    assert calls == ["create", "complete"]
    assert run_store.completed == [("run-1", "completed", [])]


def test_webhook_worker_delegates_sync_queue_calls_to_thread(tmp_path, monkeypatch):
    calls: list[str] = []

    async def fake_to_thread(func, /, *args, **kwargs):
        calls.append(getattr(func, "__name__", repr(func)))
        return func(*args, **kwargs)

    class DummyOrchestrator:
        async def handle_message(self, message):
            return {"ok": True, "message": asdict(message)}

    monkeypatch.setattr(worker_module.asyncio, "to_thread", fake_to_thread)
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue(ChannelMessage(channel="test", sender_id="u", text="hello", message_id="m1"))
    worker = WebhookWorker(queue, DummyOrchestrator())

    assert asyncio.run(worker.run_once()) is True
    assert calls == ["claim_next", "complete"]
