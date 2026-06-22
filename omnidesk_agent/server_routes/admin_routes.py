from __future__ import annotations

from typing import Awaitable, Callable, Optional

from fastapi import FastAPI, HTTPException, Request, Response

from omnidesk_agent.channels.capability_matrix import channel_capability_matrix
from omnidesk_agent.channels.ecosystem import channel_matrix, ecosystem_security_summary
from omnidesk_agent.self_learning.observability.dashboard import LearningDashboard
from omnidesk_agent.self_learning.observability.slo import IndustrialSLOEvaluator

AdminVerifier = Callable[[Request, str], Awaitable[object]]


def _ratio(numerator: float, denominator: float) -> Optional[float]:
    if denominator <= 0:
        return None
    return numerator / denominator


def _counter_sum(metrics, name: str, **labels) -> float:
    counter_sum = getattr(metrics, "counter_sum", None)
    if callable(counter_sum):
        return float(counter_sum(name, **labels))
    return 0.0


def _runtime_slo_snapshot(rt, metrics) -> dict:
    stats = rt.job_queue.stats()
    outbound = rt.outbound_messages.stats()
    total_jobs = sum(int(v) for v in stats.values())
    total_outbound = sum(int(v) for v in outbound.values())

    webhook_attempts = _counter_sum(metrics, "omnidesk_webhook_enqueue_attempts_total")
    webhook_failures = _counter_sum(metrics, "omnidesk_webhook_enqueue_failures_total")
    resume_attempts = _counter_sum(metrics, "omnidesk_resume_attempts_total")
    resume_success = _counter_sum(metrics, "omnidesk_resume_success_total")
    planner_requests = _counter_sum(metrics, "omnidesk_planner_requests_total")
    planner_fallbacks = _counter_sum(metrics, "planner_fallback_total") + _counter_sum(metrics, "omnidesk_planner_fallback_total")
    tool_calls = _counter_sum(metrics, "omnidesk_tool_calls_total")
    tool_errors = (
        _counter_sum(metrics, "omnidesk_tool_calls_total", status="error")
        + _counter_sum(metrics, "omnidesk_tool_calls_total", status="exception")
    )
    plugin_calls = _counter_sum(metrics, "omnidesk_plugin_call_total")
    plugin_timeouts = _counter_sum(metrics, "omnidesk_plugin_call_total", status="timeout")
    outbound_duplicates = _counter_sum(metrics, "omnidesk_outbound_duplicate_total")
    cost_summary = None
    store = getattr(rt, "model_cost_store", None)
    if store is not None:
        try:
            cost_summary = store.summary(days=1)
        except Exception:
            cost_summary = None

    return {
        "webhook_enqueue_success_rate": _ratio(webhook_attempts - webhook_failures, webhook_attempts),
        "job_dead_letter_rate": None if total_jobs == 0 else stats.get("dead_letter", 0) / max(total_jobs, 1),
        "approval_resume_success_rate": _ratio(resume_success, resume_attempts),
        "planner_fallback_rate": _ratio(planner_fallbacks, planner_requests),
        "tool_error_rate": _ratio(tool_errors, tool_calls),
        "outbound_duplicate_rate": _ratio(outbound_duplicates, total_outbound) if total_outbound else None,
        "plugin_timeout_rate": _ratio(plugin_timeouts, plugin_calls),
        "outbound_dead_letter_rate": None if total_outbound == 0 else outbound.get("dead_letter", 0) / max(total_outbound, 1),
        "daily_model_cost_usd": None if cost_summary is None else cost_summary.get("estimated_cost_usd"),
        "cost_per_successful_task": None if not cost_summary or stats.get("completed", 0) <= 0 else cost_summary.get("estimated_cost_usd", 0.0) / max(int(stats.get("completed", 0)), 1),
        "cache_savings_estimate_usd": None if cost_summary is None else 0.0,
    }


def register_admin_routes(app: FastAPI, cfg, rt, metrics, version: str, admin: AdminVerifier) -> None:
    @app.get("/admin/session/identity")
    async def admin_session_identity(request: Request):
        decision = await admin(request, "viewer")
        return {
            "ok": True,
            "actor": str(getattr(decision, "actor", "admin")),
            "role": str(getattr(decision, "role", "viewer")),
        }

    @app.get("/admin/status")
    async def admin_status(request: Request):
        await admin(request, "viewer")
        runtime_status = dict(rt.status())
        run_store = getattr(rt, "run_store", None)
        list_resuming = getattr(run_store, "list_resuming", None)
        if callable(list_resuming):
            runtime_status["resume_recovery"] = {"stuck_resuming_count": len(list_resuming(older_than_seconds=300, limit=1000))}
        return {"ok": True, "version": version, "runtime": runtime_status}

    @app.get("/admin/metrics")
    async def admin_metrics(request: Request):
        await admin(request, "viewer")
        return Response(content=metrics.render_prometheus(), media_type="text/plain; version=0.0.4")

    @app.get("/admin/slo")
    async def admin_slo(request: Request):
        await admin(request, "viewer")
        snapshot = _runtime_slo_snapshot(rt, metrics)
        targets = IndustrialSLOEvaluator.runtime_targets()
        return {"ok": True, "metrics": snapshot, "slo": IndustrialSLOEvaluator(targets).evaluate(snapshot)}

    @app.get("/admin/jobs")
    async def admin_jobs(request: Request, status: Optional[str] = None, limit: int = 50):
        await admin(request, "operator")
        return {"ok": True, "stats": rt.job_queue.stats(), "jobs": rt.job_queue.list(status=status, limit=limit)}

    @app.get("/admin/jobs/dead-letter")
    async def admin_dead_letter_jobs(request: Request, limit: int = 50):
        await admin(request, "operator")
        return {"ok": True, "jobs": rt.job_queue.list_dead_letters(limit=limit)}

    @app.post("/admin/jobs/dead-letter/{job_id}/requeue")
    async def admin_requeue_dead_letter_job(job_id: str, request: Request):
        await admin(request, "operator")
        try:
            return {"ok": True, **rt.job_queue.requeue_dead_letter(job_id)}
        except KeyError as exc:
            raise HTTPException(404, "job not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete("/admin/jobs/dead-letter/{job_id}")
    async def admin_purge_dead_letter_job(job_id: str, request: Request):
        await admin(request, "owner")
        try:
            return {"ok": True, **rt.job_queue.purge_dead_letter(job_id)}
        except KeyError as exc:
            raise HTTPException(404, "job not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/admin/outbound-messages")
    async def admin_outbound_messages(request: Request, status: Optional[str] = None, limit: int = 50):
        await admin(request, "operator")
        return {"ok": True, "stats": rt.outbound_messages.stats(), "messages": rt.outbound_messages.list(status=status, limit=limit)}

    @app.get("/admin/outbound")
    async def admin_outbound_alias(request: Request, status: Optional[str] = None, limit: int = 50):
        await admin(request, "operator")
        return {"ok": True, "stats": rt.outbound_messages.stats(), "messages": rt.outbound_messages.list(status=status, limit=limit)}

    @app.post("/admin/outbound/{message_id}/retry")
    async def admin_retry_outbound(message_id: str, request: Request):
        await admin(request, "operator")
        try:
            return {"ok": True, **rt.outbound_messages.requeue(message_id)}
        except KeyError as exc:
            raise HTTPException(404, "outbound message not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/admin/outbound/{message_id}/cancel")
    async def admin_cancel_outbound(message_id: str, request: Request):
        await admin(request, "operator")
        try:
            return {"ok": True, **rt.outbound_messages.cancel(message_id)}
        except KeyError as exc:
            raise HTTPException(404, "outbound message not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/admin/costs")
    async def admin_costs(request: Request, days: int = 7, group_by: Optional[str] = None):
        await admin(request, "viewer")
        store = getattr(rt, "model_cost_store", None)
        if store is None:
            return {"ok": True, "costs": None}
        return {"ok": True, "costs": store.summary(days=days, group_by=group_by)}

    @app.get("/admin/costs/by-provider")
    async def admin_costs_by_provider(request: Request, days: int = 7):
        await admin(request, "viewer")
        return {"ok": True, "costs": rt.model_cost_store.summary(days=days, group_by="provider")}

    @app.get("/admin/costs/by-actor")
    async def admin_costs_by_actor(request: Request, days: int = 7):
        await admin(request, "viewer")
        return {"ok": True, "costs": rt.model_cost_store.summary(days=days, group_by="actor")}

    @app.get("/admin/costs/by-task")
    async def admin_costs_by_task(request: Request, days: int = 7):
        await admin(request, "viewer")
        return {"ok": True, "costs": rt.model_cost_store.summary(days=days, group_by="task")}

    @app.get("/admin/channels/ecosystem")
    async def admin_channel_ecosystem(request: Request, include_reference: bool = True):
        await admin(request, "viewer")
        return {
            "ok": True,
            "channels": channel_matrix(include_reference=include_reference),
            "capabilities": channel_capability_matrix(include_reference=include_reference),
            "security": ecosystem_security_summary(include_reference=include_reference),
        }

    def learning_dashboard() -> LearningDashboard:
        return LearningDashboard.from_audit_path(cfg.workspace.root / "learning_audit.jsonl")


    @app.get("/admin/memory/purge-expired")
    async def admin_memory_purge_expired_dry_run(
        request: Request,
        dry_run: bool = True,
        limit: int = 1000,
        channel: Optional[str] = None,
        actor: Optional[str] = None,
    ):
        await admin(request, "operator")
        memory = getattr(rt, "memory", None)
        if memory is None or not hasattr(memory, "purge_expired"):
            raise HTTPException(404, "memory store not available")
        return {"ok": True, "purge": memory.purge_expired(dry_run=dry_run, limit=limit, channel=channel, actor=actor)}

    @app.post("/admin/memory/purge-expired")
    async def admin_memory_purge_expired_execute(
        request: Request,
        dry_run: bool = False,
        limit: int = 1000,
        channel: Optional[str] = None,
        actor: Optional[str] = None,
    ):
        await admin(request, "owner")
        memory = getattr(rt, "memory", None)
        if memory is None or not hasattr(memory, "purge_expired"):
            raise HTTPException(404, "memory store not available")
        return {"ok": True, "purge": memory.purge_expired(dry_run=dry_run, limit=limit, channel=channel, actor=actor)}

    @app.get("/admin/learning/report")
    async def admin_learning_report(request: Request, days: int = 7):
        await admin(request, "viewer")
        return learning_dashboard().summary(days=days)

    @app.get("/admin/learning/dashboard")
    async def admin_learning_dashboard(request: Request, days: int = 7):
        await admin(request, "viewer")
        return Response(content=learning_dashboard().render_html(days=days), media_type="text/html")
