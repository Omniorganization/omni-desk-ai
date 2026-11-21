"""The post-deploy SLO gate must pass a healthy fresh deployment and block real breaches.

Why: the gate failed on every freshly started process, because a check with no
data yet (``missing_data``) counted as a failure, and so did warnings. Staging,
production and rollback runs then failed right after changing the live service.
They have no automatic rollback in compose/kubectl mode, so operators would
have to bypass the gate. docs/SLO.md says critical violations block promotion;
these tests pin that contract.
"""
from __future__ import annotations

from types import SimpleNamespace

from omnidesk_agent.observability import MetricsRegistry, initialize_runtime_metrics
from omnidesk_agent.self_learning.observability.slo import IndustrialSLOEvaluator
from omnidesk_agent.server_routes.admin_routes import _runtime_slo_snapshot
from scripts import production_smoke_test


def _admin_slo_payload(metrics: MetricsRegistry, *, jobs: dict | None = None, outbound: dict | None = None) -> dict:
    runtime = SimpleNamespace(
        job_queue=SimpleNamespace(stats=lambda: dict(jobs or {})),
        outbound_messages=SimpleNamespace(stats=lambda: dict(outbound or {})),
        model_cost_store=None,
    )
    snapshot = _runtime_slo_snapshot(runtime, metrics)
    return {"ok": True, "metrics": snapshot, "slo": IndustrialSLOEvaluator(IndustrialSLOEvaluator.runtime_targets()).evaluate(snapshot)}


def test_freshly_started_gateway_passes_the_gate_and_reports_missing_data() -> None:
    metrics = MetricsRegistry()
    initialize_runtime_metrics(metrics)
    payload = _admin_slo_payload(metrics)
    gate = production_smoke_test._slo_gate(payload)
    assert gate["ok"] is True
    assert gate["blocking"] == []
    # Nothing is hidden: every objective without data is listed for the operator.
    assert "webhook_enqueue_success_rate" in gate["missing_data"]
    assert len(gate["missing_data"]) == gate["checked"]


def test_critical_violation_blocks_promotion() -> None:
    metrics = MetricsRegistry()
    initialize_runtime_metrics(metrics)
    metrics.inc("omnidesk_outbound_duplicate_total", 1)
    payload = _admin_slo_payload(metrics, outbound={"sent": 10})
    gate = production_smoke_test._slo_gate(payload)
    assert gate["ok"] is False
    assert [item["metric"] for item in gate["blocking"]] == ["outbound_duplicate_rate"]


def test_error_violation_blocks_but_warning_only_reports() -> None:
    metrics = MetricsRegistry()
    initialize_runtime_metrics(metrics)
    metrics.inc("omnidesk_webhook_enqueue_attempts_total", 100)
    metrics.inc("omnidesk_webhook_enqueue_failures_total", 5)  # 95% < 99.9% (error severity)
    metrics.inc("omnidesk_tool_calls_total", 10, status="ok")
    metrics.inc("omnidesk_tool_calls_total", 10, status="error")  # 50% > 2% (warning severity)
    gate = production_smoke_test._slo_gate(_admin_slo_payload(metrics))
    assert [item["metric"] for item in gate["blocking"]] == ["webhook_enqueue_success_rate"]
    assert [item["metric"] for item in gate["warnings"]] == ["tool_error_rate"]


def test_gate_refuses_a_payload_without_checks() -> None:
    assert production_smoke_test._slo_gate({"ok": True, "slo": "unexpected"})["ok"] is False


def test_fail_on_slo_raises_only_for_blocking_violations(monkeypatch) -> None:
    metrics = MetricsRegistry()
    initialize_runtime_metrics(metrics)
    fresh = _admin_slo_payload(metrics)

    def fake_request_json(url: str, token: str | None = None) -> dict:
        if url.endswith("/health"):
            return {"ok": True, "version": "1.0"}
        if url.endswith("/admin/status"):
            return {"ok": True}
        return fresh

    monkeypatch.setenv("OMNIDESK_SMOKE_ADMIN_TOKEN", "t" * 40)
    monkeypatch.setattr(production_smoke_test, "request_json", fake_request_json)
    result = production_smoke_test.check_app(check_admin_slo=True, fail_on_slo=True)
    assert result["slo"]["gate"]["ok"] is True


def test_strict_sandbox_without_a_runner_url_fails_instead_of_skipping(monkeypatch, capsys) -> None:
    monkeypatch.delenv("OMNIDESK_SMOKE_SANDBOX_URL", raising=False)
    monkeypatch.setattr(production_smoke_test, "check_app", lambda **_: {"health": {"ok": True}})
    assert production_smoke_test.main(["--strict-sandbox"]) == 1
    assert "--strict-sandbox requires OMNIDESK_SMOKE_SANDBOX_URL" in capsys.readouterr().err
