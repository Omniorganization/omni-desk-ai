# OpenTelemetry

Source assets:

- `deploy/observability/otel-collector.yaml`
- `deploy/observability/docker-compose.otel.yml`
- `deploy/observability/prometheus.yml`
- `deploy/observability/prometheus-rules.yml`
- `deploy/observability/grafana-dashboard.json`
- `deploy/observability/tempo.yaml`
- `omnidesk_agent/observability_otel.py`
- `omnidesk_agent/observability_tracing.py`
- `scripts/check_observability_collector_contract.py`
- `docs/OBSERVABILITY_OTEL_PRODUCTION_CHAIN.md`

Required trace dimensions include tenant, user, session, agent, channel, model provider, tool, approval, memory hit, latency, token usage, and error code.

Validation:

```bash
python scripts/check_observability_collector_contract.py .
```

Production-equivalent local stack:

```bash
docker compose -f deploy/observability/docker-compose.otel.yml up -d
```

The Compose stack intentionally requires digest-pinned images through environment variables. It does not replace live Real GA telemetry evidence; it only provides the deployable collector/exporter chain needed to produce that evidence.
