# OpenTelemetry Production Observability Chain

This document closes the source-side P1 observability gap by wiring the existing runtime span exporter to a deployable collector stack. It does not claim that production telemetry has already been captured; operators must still run the stack in staging or production and attach evidence to the Real GA evidence set.

## Runtime path

```text
FastAPI request / runtime span
  -> trace_span(...)
  -> AsyncOTLPHttpExporter
  -> OMNIDESK_OTLP_ENDPOINT
  -> OpenTelemetry Collector
  -> Tempo trace backend
  -> Prometheus rules and Grafana dashboard
```

## Required environment

Set the application OTLP endpoint to the collector HTTP receiver:

```bash
export OMNIDESK_OTLP_ENDPOINT=http://127.0.0.1:4318/v1/traces
```

When the app runs inside the same Compose network as the collector, use:

```bash
export OMNIDESK_OTLP_ENDPOINT=http://otel-collector:4318/v1/traces
```

## Deploy the local production-equivalent stack

The Compose file requires digest-pinned images. Example variable names:

```bash
export OMNIDESK_OTEL_COLLECTOR_IMAGE=otel/opentelemetry-collector-contrib@sha256:<digest>
export OMNIDESK_TEMPO_IMAGE=grafana/tempo@sha256:<digest>
export OMNIDESK_PROMETHEUS_IMAGE=prom/prometheus@sha256:<digest>
export OMNIDESK_GRAFANA_IMAGE=grafana/grafana@sha256:<digest>
export OMNIDESK_GRAFANA_ADMIN_USER=admin
export OMNIDESK_GRAFANA_ADMIN_PASSWORD_FILE=/absolute/path/to/grafana-password
export OMNIDESK_METRICS_TOKEN_FILE=/absolute/path/to/omnidesk-viewer-or-operator-token

docker compose -f deploy/observability/docker-compose.otel.yml up -d
```

## Validate source-side contract

```bash
python scripts/check_observability_collector_contract.py .
```

This confirms that:

- OTLP HTTP and gRPC receivers exist.
- memory limiter and batch processors are configured.
- the Tempo exporter is present.
- a digest-pinned Compose stack exists.
- the server wires `OMNIDESK_OTLP_ENDPOINT` to the async exporter.
- `traceparent` is propagated back to callers.

## Evidence required for Real GA

For Real GA, attach an operator-produced evidence document, such as:

```text
release/external-evidence/observability/otel-live-trace-export.json
```

Minimum contents:

```json
{
  "schema": "omnidesk-otel-live-trace-export/v1",
  "status": "passed",
  "produced_at": "ISO-8601 timestamp",
  "producer": "CI run or operator identity",
  "environment": "staging",
  "collector_endpoint": "http://otel-collector:4318/v1/traces",
  "trace_id": "real trace id observed in Tempo",
  "request_id": "gateway request id",
  "span_count": 3,
  "tempo_query_verified": true,
  "prometheus_rules_loaded": true,
  "grafana_dashboard_loaded": true
}
```

This source change only provides the collector/exporter chain. It does not replace the required live telemetry evidence.
