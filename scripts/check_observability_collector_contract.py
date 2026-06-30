#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


REQUIRED_COLLECTOR_TERMS = [
    "receivers:",
    "otlp:",
    "protocols:",
    "http:",
    "grpc:",
    "processors:",
    "memory_limiter:",
    "batch:",
    "exporters:",
    "otlp/tempo:",
    "service:",
    "pipelines:",
    "traces:",
]

REQUIRED_COMPOSE_TERMS = [
    "OMNIDESK_OTEL_COLLECTOR_IMAGE",
    "OMNIDESK_TEMPO_IMAGE",
    "OMNIDESK_PROMETHEUS_IMAGE",
    "OMNIDESK_GRAFANA_IMAGE",
    "otel-collector.yaml",
    "prometheus-rules.yml",
    "grafana-dashboard.json",
    "4317",
    "4318",
]


def _read(path: Path, issues: list[str]) -> str:
    if not path.exists():
        issues.append(f"missing file: {path}")
        return ""
    return path.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the production OpenTelemetry collector/exporter chain assets.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    issues: list[str] = []

    collector = root / "deploy" / "observability" / "otel-collector.yaml"
    compose = root / "deploy" / "observability" / "docker-compose.otel.yml"
    server = root / "omnidesk_agent" / "server.py"
    otel = root / "omnidesk_agent" / "observability_otel.py"
    tracing = root / "omnidesk_agent" / "observability_tracing.py"
    production_config = root / "examples" / "config.production.yaml"

    collector_text = _read(collector, issues)
    for term in REQUIRED_COLLECTOR_TERMS:
        if term not in collector_text:
            issues.append(f"collector config missing {term}")
    if not re.search(r"endpoint:\s*0\.0\.0\.0:4318", collector_text):
        issues.append("collector must listen for OTLP/HTTP on 0.0.0.0:4318")
    if not re.search(r"endpoint:\s*0\.0\.0\.0:4317", collector_text):
        issues.append("collector must listen for OTLP/gRPC on 0.0.0.0:4317")

    compose_text = _read(compose, issues)
    for term in REQUIRED_COMPOSE_TERMS:
        if term not in compose_text:
            issues.append(f"OTel compose stack missing {term}")
    for env_name in ("OMNIDESK_OTEL_COLLECTOR_IMAGE", "OMNIDESK_TEMPO_IMAGE", "OMNIDESK_PROMETHEUS_IMAGE", "OMNIDESK_GRAFANA_IMAGE"):
        pattern = re.compile(r"\$\{" + re.escape(env_name) + r":\?[^}]*sha256", re.IGNORECASE)
        if not pattern.search(compose_text):
            issues.append(f"{env_name} must be required with a digest-pinned value")

    server_text = _read(server, issues)
    if "AsyncOTLPHttpExporter" not in server_text or "OMNIDESK_OTLP_ENDPOINT" not in (server_text + _read(production_config, issues)):
        issues.append("server/config must wire OMNIDESK_OTLP_ENDPOINT to AsyncOTLPHttpExporter")
    if "traceparent" not in server_text:
        issues.append("server must propagate traceparent headers")

    otel_text = _read(otel, issues)
    tracing_text = _read(tracing, issues)
    if "resourceSpans" not in otel_text or "OTLPHttpExporter" not in otel_text:
        issues.append("observability_otel.py must emit OTLP JSON resourceSpans through OTLPHttpExporter")
    if "trace_span" not in tracing_text or "otel_exporter.export" not in tracing_text:
        issues.append("observability_tracing.py must export spans through the configured OTLP exporter")

    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("observability collector/exporter contract verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
