# OpenTelemetry

Source assets:

- `deploy/observability/otel-collector.yaml`
- `deploy/observability/prometheus-rules.yml`
- `deploy/observability/grafana-dashboard.json`
- `omnidesk_agent/observability_otel.py`
- `omnidesk_agent/observability_tracing.py`

Required trace dimensions include tenant, user, session, agent, channel, model provider, tool, approval, memory hit, latency, token usage, and error code.
