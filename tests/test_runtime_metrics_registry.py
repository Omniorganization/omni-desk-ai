from __future__ import annotations

from omnidesk_agent.observability import DEFAULT_RUNTIME_METRICS, MetricsRegistry, initialize_runtime_metrics


def test_runtime_metrics_are_initialized():
    metrics = MetricsRegistry()
    initialize_runtime_metrics(metrics)
    rendered = metrics.render_prometheus()
    for name in DEFAULT_RUNTIME_METRICS:
        assert name in rendered
