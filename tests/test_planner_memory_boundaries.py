from __future__ import annotations

import asyncio
import json
import time

import pytest

from omnidesk_agent.config import MemoryPrivacyConfig
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.core.structured_planner import LLMStructuredPlanner
from omnidesk_agent.core.token_budget import TokenBudgetConfig
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.models.base import ModelResponse
from omnidesk_agent.repositories.postgres_state import PostgresExperienceStore, PostgresOutboundMessageStore, PostgresTokenBudgetManager
from test_postgres_state_runtime_contracts import MemoryJsonState


def _experience(**overrides):
    return {"task_type": "workflow", "goal": "shipment", "success": True,
            "risk_level": "low", "human_feedback": "private customer note", **overrides}


@pytest.fixture(params=["sqlite", "postgres"])
def memory(request, tmp_path):
    cfg = MemoryPrivacyConfig()
    store = ExperienceStore(tmp_path / "memory.sqlite3", cfg) if request.param == "sqlite" else PostgresExperienceStore(MemoryJsonState(), cfg)
    try:
        yield store
    finally:
        store.close()


def test_original_actor_context_does_not_receive_another_actors_memory(tmp_path):
    # Compatibility adapter permits the same dataflow test on the immutable old API.
    with ExperienceStore(tmp_path / "memory.sqlite3") as store:
        store.add_experience(_experience(), channel="chat", actor="alice")
        store.add_experience(_experience(human_feedback="other actor confidential"), channel="chat", actor="bob")
        try:
            rows = store.retrieve_for_task("shipment", channel="chat", actor="alice")
        except TypeError:
            rows = store.retrieve_for_task("shipment")
        assert rows and all(row["actor"] == "alice" for row in rows)


def test_original_postgres_budget_honors_call_cap_without_sqlite_path():
    state = MemoryJsonState()
    state.put("llm_usage", "call", {"task_id": "task", "created_at": time.time()})
    budget = PostgresTokenBudgetManager(state, TokenBudgetConfig(per_task_max_llm_calls=1))
    decision = budget.decide(model="synthetic", system="instructions", user="task", task_id="task", verified_required=True)
    assert not decision.allowed and "call limit" in decision.reason


def test_original_retry_rechecks_sent_state_inside_the_locked_update(monkeypatch):
    state = MemoryJsonState()
    store = PostgresOutboundMessageStore(state)
    message_id = store.create(channel="chat", recipient="synthetic", payload={"text": "synthetic"})
    original = state.update_locked

    def interleaved(namespace, key, updater):
        state.rows[namespace][key]["status"] = "sent"
        state.rows[namespace][key]["provider_message_id"] = "already-delivered"
        return original(namespace, key, updater)

    monkeypatch.setattr(state, "update_locked", interleaved)
    with pytest.raises(ValueError):
        store.requeue(message_id)
    assert store.get(message_id)["status"] == "sent"


@pytest.mark.parametrize("channel,actor", [(None, None), ("chat", None), (None, "alice"), ("chat", "unknown"), ("", "alice")])
def test_missing_owner_context_fails_closed(memory, channel, actor):
    memory.add_experience(_experience(), channel="chat", actor="alice")
    assert memory.retrieve_for_task("shipment", channel=channel, actor=actor) == []


def test_channel_actor_and_review_expiry_are_checked_before_decryption(memory, monkeypatch):
    live = memory.add_experience(_experience(), channel="chat", actor="alice")
    memory.add_experience(_experience(), channel="chat", actor="bob")
    memory.add_experience(_experience(), channel="mail", actor="alice")
    memory.add_experience(_experience())
    memory.add_experience(_experience(expires_at=time.time() - 1), channel="chat", actor="alice")
    for status in ("blocked", "deprecated"):
        eid = memory.add_experience(_experience(), channel="chat", actor="alice")
        memory.update_memory_review(eid, memory_status=status, confidence=0.2)
    original = memory._decode_structured
    decoded_ids = []

    def decode(row):
        decoded_ids.append(row["id"])
        return original(row)

    monkeypatch.setattr(memory, "_decode_structured", decode)
    rows = memory.retrieve_for_task("shipment", channel="chat", actor="alice")
    assert [row["id"] for row in rows] == [live]
    assert set(decoded_ids) == {live}
    assert rows[0]["human_feedback"] == "private customer note"


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_encrypted_memories_keep_actor_scope(tmp_path, monkeypatch, backend):
    monkeypatch.setenv("OMNIDESK_MEMORY_ENCRYPTION_KEY", "synthetic-acceptance-key")
    cfg = MemoryPrivacyConfig(encrypt_at_rest=True)
    store = ExperienceStore(tmp_path / "encrypted.sqlite3", cfg) if backend == "sqlite" else PostgresExperienceStore(MemoryJsonState(), cfg)
    try:
        store.add_experience(_experience(), channel="chat", actor="alice")
        store.add_experience(_experience(goal="shipment foreign"), channel="chat", actor="bob")
        rows = store.retrieve_for_task("shipment", channel="chat", actor="alice")
        assert len(rows) == 1 and rows[0]["actor"] == "alice" and rows[0]["goal"] == "shipment"
    finally:
        store.close()


def test_postgres_touch_preserves_concurrent_curator_block(monkeypatch):
    state = MemoryJsonState()
    store = PostgresExperienceStore(state)
    eid = store.add_experience(_experience(), channel="chat", actor="alice")
    original = state.update_locked

    def block(namespace, key, updater):
        state.rows[namespace][key]["memory_status"] = "blocked"
        return original(namespace, key, updater)

    monkeypatch.setattr(state, "update_locked", block)
    assert store.retrieve_for_task("shipment", channel="chat", actor="alice") == []
    assert state.get(store.namespace_structured, str(eid))["memory_status"] == "blocked"


@pytest.mark.parametrize("expiry", [True, "tomorrow", float("nan"), float("inf"), 0])
def test_malformed_or_expired_retention_is_not_planner_context(expiry):
    from omnidesk_agent.memory.retrieval import planner_memory_visible

    row = {"channel": "chat", "actor": "alice", "memory_status": "trusted", "expires_at": expiry}
    assert not planner_memory_visible(row, channel="chat", actor="alice", now=time.time())


def test_structured_planner_provider_context_is_actor_scoped(memory):
    memory.add_experience(_experience(), channel="chat", actor="alice")
    memory.add_experience(_experience(goal="shipment foreign", human_feedback="other actor confidential"), channel="chat", actor="bob")

    class Router:
        requests = []

        async def complete(self, request):
            self.requests.append(request)
            return ModelResponse(text="{}", provider="synthetic", model="synthetic", profile="planner")

    class Skills:
        def prompt_block(self, *args, **kwargs):
            return ""

    class Tools:
        def describe(self):
            return {}

    class Fallback:
        async def plan(self, msg):
            return None

    router = Router()
    planner = LLMStructuredPlanner(router, memory, Skills(), Tools(), Fallback())
    asyncio.run(planner.plan(ChannelMessage(channel="chat", sender_id="alice", thread_id="t", message_id="m", text="shipment")))
    context = json.loads(router.requests[0].user)["recent_experiences"]
    assert context and all(row["actor"] == "alice" for row in context)
    assert "other actor confidential" not in router.requests[0].user
