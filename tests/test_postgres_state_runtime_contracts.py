from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

import pytest

from omnidesk_agent.config import MemoryPrivacyConfig
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.core.token_budget import TokenBudgetConfig
from omnidesk_agent.repositories.postgres_state import (
    PostgresApprovalStore,
    PostgresBreakGlassStore,
    PostgresDualApprovalStore,
    PostgresExperimentManager,
    PostgresExperienceStore,
    PostgresJobQueue,
    PostgresModelCostStore,
    PostgresOutboundMessageStore,
    PostgresRunStore,
    PostgresRuntimeStateStores,
    PostgresTokenBudgetManager,
    PostgresWebhookSecurity,
    _loads,
)
from omnidesk_agent.security.webhook_security import WebhookSecurityConfig
from omnidesk_agent.self_learning.experiments import ExperimentObservation, ExperimentSpec


class MemoryJsonState:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, dict[str, Any]]] = {}

    def put(self, namespace: str, key: str, value: dict[str, Any]) -> None:
        self.rows.setdefault(namespace, {})[str(key)] = dict(value)

    def insert_once(self, namespace: str, key: str, value: dict[str, Any]) -> bool:
        bucket = self.rows.setdefault(namespace, {})
        key = str(key)
        if key in bucket:
            return False
        bucket[key] = dict(value)
        return True

    def get(self, namespace: str, key: str) -> Optional[dict[str, Any]]:
        row = self.rows.get(namespace, {}).get(str(key))
        return dict(row) if row is not None else None

    def delete(self, namespace: str, key: str) -> bool:
        return self.rows.get(namespace, {}).pop(str(key), None) is not None

    def list(self, namespace: str, *, status: Optional[str] = None, limit: int = 50, ascending: bool = False) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self.rows.get(namespace, {}).values()]
        if status is not None:
            rows = [row for row in rows if row.get("status") == status]
        rows.sort(key=lambda row: float(row.get("created_at") or 0), reverse=not ascending)
        return rows[:limit]

    def stats_by_status(self, namespace: str) -> dict[str, int]:
        stats: dict[str, int] = {}
        for row in self.rows.get(namespace, {}).values():
            status = row.get("status")
            if status is not None:
                stats[str(status)] = stats.get(str(status), 0) + 1
        return stats

    def find_by_field(self, namespace: str, field: str, value: str) -> Optional[dict[str, Any]]:
        if field not in {"id", "dedupe_key", "idempotency_key", "status", "waiting_approval_id"}:
            raise ValueError(f"unsupported JSON field lookup: {field}")
        for row in self.rows.get(namespace, {}).values():
            if str(row.get(field)) == str(value):
                return dict(row)
        return None

    def update_locked_by_field(self, namespace: str, field: str, value: str, updater) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        if field not in {"id", "dedupe_key", "idempotency_key", "status", "waiting_approval_id"}:
            raise ValueError(f"unsupported JSON field update: {field}")
        for key, row in self.rows.get(namespace, {}).items():
            if str(row.get(field)) == str(value):
                updated = updater(dict(row))
                self.rows[namespace][key] = dict(updated)
                return dict(updated)
        raise KeyError(value)

    def update_locked(self, namespace: str, key: str, updater) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        key = str(key)
        if key not in self.rows.get(namespace, {}):
            raise KeyError(key)
        updated = updater(dict(self.rows[namespace][key]))
        self.rows[namespace][key] = dict(updated)
        return dict(updated)

    def claim_one(self, namespace: str, predicate, updater) -> Optional[dict[str, Any]]:  # type: ignore[no-untyped-def]
        for key, row in sorted(self.rows.get(namespace, {}).items(), key=lambda item: float(item[1].get("created_at") or 0)):
            current = dict(row)
            if not predicate(current):
                continue
            self.rows[namespace][key] = dict(updater(dict(current)))
            return current
        return None


def _runtime_with_state(state: MemoryJsonState) -> PostgresRuntimeStateStores:
    runtime = PostgresRuntimeStateStores.__new__(PostgresRuntimeStateStores)
    runtime.state = state
    runtime._dual = None
    return runtime


def test_postgres_state_loads_and_runtime_factories(tmp_path: Path) -> None:
    state = MemoryJsonState()
    assert _loads({"ok": True}) == {"ok": True}
    assert _loads('{"ok": true}') == {"ok": True}
    runtime = _runtime_with_state(state)
    assert runtime.dual_approval_store() is runtime.dual_approval_store()
    assert runtime.approval_store(ttl_seconds=30) is not None
    assert runtime.break_glass_store(audit_log=tmp_path / "audit.jsonl") is not None
    assert runtime.run_store() is not None
    assert runtime.job_queue() is not None
    assert runtime.outbound_messages() is not None
    assert runtime.webhook_security() is not None
    assert runtime.learning_experiments() is not None
    assert runtime.memory_store() is not None
    assert runtime.token_budget_manager() is not None
    assert runtime.model_cost_store() is not None
    assert runtime.health_check()["ok"] is True


def test_postgres_approval_run_breakglass_and_webhook_contracts(tmp_path: Path) -> None:
    state = MemoryJsonState()
    dual = PostgresDualApprovalStore(state)
    approvals = PostgresApprovalStore(state, ttl_seconds=30, dual_approval_store=dual)
    approval_id = approvals.create({"tool": "shell", "created_by": "author", "requires_dual_approval": True, "scope_hash": "s1"})
    with pytest.raises(PermissionError):
        approvals.decide(approval_id, "approved")
    with pytest.raises(PermissionError):
        dual.approve(approval_id, "author")
    first = dual.approve(approval_id, "reviewer-a")
    assert first.ready is False
    with pytest.raises(PermissionError):
        dual.approve(approval_id, "reviewer-a")
    assert dual.approve(approval_id, "reviewer-b").ready is True
    assert approvals.decide(approval_id, "approved", {"ok": True})["status"] == "approved"
    assert approvals.require_approved(approval_id, {"scope_hash": "s1"})["id"] == approval_id
    assert approvals.consume_approved(approval_id, {"scope_hash": "s1"}, consumed_by_run_id="run-1")["status"] == "consumed"
    with pytest.raises(PermissionError):
        approvals.require_approved(approval_id)

    runs = PostgresRunStore(state)
    run_id = runs.create({"channel": "unit", "text": "hello"})
    token = runs.save_waiting(run_id, {"goal": "approve"}, 1, [{"ok": True}], approval_id, {"scope_hash": "s1"})
    with pytest.raises(PermissionError):
        runs.require_resume_token(run_id, "bad-token")
    runs.require_resume_token(run_id, token)
    runs.consume_resume_token(run_id, token)
    assert runs.list_resuming(older_than_seconds=0, limit=10)
    runs.mark_resume_failed(run_id, "boom")
    assert runs.get(run_id)["status"] == "resume_failed"
    with pytest.raises(ValueError):
        runs.update(run_id, {"unknown": True})
    runs.complete(run_id, "completed", [{"ok": True}])
    assert runs.get_by_approval(approval_id) is None

    breakglass = PostgresBreakGlassStore(state, audit_log=tmp_path / "audit.jsonl")
    with pytest.raises(PermissionError):
        breakglass.open(session_id="bg0", actor="alice", approved_by="alice", reason="same")
    session = breakglass.open(session_id="bg1", actor="alice", approved_by="bob", reason="restore", ttl_seconds=60)
    assert session.active is True
    with pytest.raises(PermissionError):
        breakglass.assert_active("bg1", actor="mallory")
    breakglass.revoke("bg1", revoked_by="bob")
    with pytest.raises(PermissionError):
        breakglass.assert_active("bg1", actor="alice")
    assert "break_glass.revoke" in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")

    webhook = PostgresWebhookSecurity(state, WebhookSecurityConfig(replay_ttl_seconds=30, rate_limit_max_requests=2))
    assert webhook.guard(channel="x", body=b"body", source_key="src", message_id="m1")["ok"] is True
    with pytest.raises(PermissionError):
        webhook.guard(channel="x", body=b"body", source_key="src", message_id="m1")
    with pytest.raises(PermissionError):
        webhook.guard(channel="x", body=b"old", source_key="other", timestamp=time.time() - 1000)


def test_postgres_job_and_outbound_state_transitions() -> None:
    state = MemoryJsonState()
    queue = PostgresJobQueue(state, max_retries=0, base_retry_seconds=1)
    message = ChannelMessage(channel="unit", sender_id="u", thread_id="t", message_id="m1", text="hello")
    created = queue.enqueue(message)
    assert created["created"] is True
    assert queue.enqueue(message)["created"] is False
    claimed = queue.claim_next()
    assert claimed and claimed["status"] == "pending"
    state.update_locked_by_field("jobs", "id", created["job_id"], lambda row: {**row, "locked_at": time.time() - 60})
    assert queue.recover_stale_running(lease_seconds=1) == 1
    assert queue.get(created["job_id"])["status"] == "dead_letter"
    assert queue.requeue_dead_letter(created["job_id"])["status"] == "pending"
    queue.fail(created["job_id"], "hard fail")
    assert queue.list_dead_letters(limit=10)
    assert queue.purge_dead_letter(created["job_id"])["purged"] is True

    outbound = PostgresOutboundMessageStore(state, max_retries=0, base_retry_seconds=1)
    message_id = outbound.create(channel="email", recipient="r", payload={"text": "hi"}, idempotency_key="idem-1")
    assert outbound.create(channel="email", recipient="r", payload={"text": "hi"}, idempotency_key="idem-1") == message_id
    assert outbound.claim_next()["id"] == message_id
    assert outbound.mark_ambiguous(message_id, "unknown", provider_request_id="req-1")["requires_reconciliation"] is True
    assert outbound.list_ambiguous(limit=10)
    assert outbound.requeue(message_id)["status"] == "pending"
    outbound.claim_next()
    assert outbound.mark_failed(message_id, "failed")["status"] == "dead_letter"
    assert outbound.recover_stale_running(lease_seconds=1) == 0
    assert outbound.stats()["dead_letter"] == 1
    assert outbound.find_by_idempotency_key("idem-1")["id"] == message_id
    assert outbound.cancel(outbound.create(channel="sms", recipient="r", payload={"text": "bye"}))["status"] == "cancelled"
    sent_id = outbound.create(channel="push", recipient="r", payload={"text": "sent"})
    outbound.mark_sent(sent_id, provider_message_id="p1", provider_request_id="r1")
    with pytest.raises(ValueError):
        outbound.requeue(sent_id)


def test_postgres_learning_memory_token_and_cost_contracts() -> None:
    state = MemoryJsonState()
    budget = PostgresTokenBudgetManager(state, TokenBudgetConfig(enable_cache=True, cache_ttl_seconds=60))
    cache_key = budget.make_cache_key("gpt", "system", "user")
    budget.put_cached(cache_key=cache_key, model="gpt", response="cached")
    assert budget.get_cached(cache_key) == "cached"
    budget.record_call(
        task_id="task-1",
        model="gpt",
        estimated_input_tokens=10,
        estimated_output_tokens=5,
        verified_required=True,
        budget_overridden=False,
        reason="unit",
    )

    costs = PostgresModelCostStore(state)
    costs.record(task_id="task-1", provider="openai", model="gpt", profile="planner", input_tokens=10, output_tokens=5, estimated_cost_usd=0.12, cache_hit=True)
    costs.record(task_id="task-2", provider="openai", model="gpt", profile="planner", input_tokens=20, output_tokens=10, estimated_cost_usd=0.24)
    summary = costs.summary(days=1, group_by="provider")
    assert summary["calls"] == 2
    assert summary["groups"]["openai"]["estimated_cost_usd"] == pytest.approx(0.36)

    experiments = PostgresExperimentManager(state)
    experiments.create(ExperimentSpec("exp-1", "Policy", "old", "new", treatment_percent=50))
    assert experiments.get("exp-1")["name"] == "Policy"
    assert experiments.assign("exp-1", "customer-1").arm in {"control", "treatment"}
    experiments.record(ExperimentObservation("exp-1", "u1", "control", True, reward=1, cost=1))
    experiments.record(ExperimentObservation("exp-1", "u2", "treatment", False, reward=0, cost=2, safety_violation=True))
    assert len(experiments.observations("exp-1")) == 2
    assert experiments.summary("exp-1")["control"]["sample_count"] == 1.0
    with pytest.raises(ValueError):
        experiments.create(ExperimentSpec("bad", "Bad", "old", "new", treatment_percent=101))
    with pytest.raises(ValueError):
        experiments.assign("missing", "u")

    memory = PostgresExperienceStore(state, privacy_config=MemoryPrivacyConfig(encrypt_at_rest=False))
    legacy_id = memory.add("fix failed deployment", "roll back", "success", ["ops"])
    assert legacy_id > 0
    assert memory.search("deployment", limit=3)
    exp_id = memory.add_experience(
        {
            "task_type": "incident",
            "goal": "resolve api outage",
            "success": False,
            "failure_reason": "bad deploy",
            "solution_attempted": ["rollback"],
            "recommended_next_action": "verify health",
            "risk_level": "high",
            "reusable_skill": True,
            "tags": ["api", "ops"],
            "raw_trace": {"step": 1},
            "expires_at": time.time() - 1,
            "privacy_level": "normal",
        },
        channel="slack",
        actor="operator",
    )
    assert exp_id > 0
    assert memory.search_similar("outage", only_reusable=True)
    assert memory.retrieve_for_task("outage")
    assert memory.summarize_failures(days=1)[0]["failure_reason"] == "bad deploy"
    assert memory.list_structured(statuses=["candidate"])
    memory.update_memory_review(exp_id, memory_status="validated", confidence=0.9, reason="works")
    assert memory.purge_expired(dry_run=False)["deleted_count"] == 1
    memory.record_metric(success=True, skill_reuse=True)
    report = memory.metrics_report(days=1)
    assert report["task_count"] == 1
    assert report["success_rate"] == 1.0
