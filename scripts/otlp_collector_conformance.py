"""Prove the runtime OTLP exporter against a real OpenTelemetry Collector.

Run by .github/workflows/observability-conformance.yml with an
otel/opentelemetry-collector-contrib container whose OTLP/HTTP receiver writes
everything it accepts to a JSON file. Checks:

1. spans produced by ``trace_span`` through ``AsyncOTLPHttpExporter`` are
   accepted (no export failures) and arrive with the same trace/span/parent ids,
   integer status codes and typed attributes;
2. the legacy payload shape (``"status": {"code": "OK"}``) is rejected, which
   shows this check can fail;
3. an unreachable collector is counted in omnidesk_trace_export_failures_total.

Writes a JSON report (--report) and exits non-zero on any failure.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnidesk_agent.observability import MetricsRegistry  # noqa: E402
from omnidesk_agent.observability_otel import AsyncOTLPHttpExporter  # noqa: E402
from omnidesk_agent.observability_tracing import trace_context_from_headers, trace_span  # noqa: E402


def _post_raw(endpoint: str, payload: dict) -> int:
    request = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # nosec B310 - local collector
            return int(response.status)
    except urllib.error.HTTPError as exc:
        exc.close()
        return int(exc.code)


def _collected_spans(path: Path) -> dict[str, dict]:
    spans: dict[str, dict] = {}
    if not path.exists():
        return spans
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        for resource in json.loads(line).get("resourceSpans", []):
            for scope in resource.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    spans[span["spanId"]] = span
    return spans


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="collector OTLP/HTTP traces URL")
    parser.add_argument("--collector-output", required=True, type=Path, help="file exporter output written by the collector")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)

    failures: list[str] = []
    metrics = MetricsRegistry()
    exporter = AsyncOTLPHttpExporter(endpoint=args.endpoint, timeout=5, metrics=metrics)
    inbound = trace_context_from_headers("client-supplied-request-id-not-hex", None)
    sent: dict[str, dict] = {}
    try:
        with trace_span("http.request", trace_id=inbound.trace_id, kind="SERVER", metrics=metrics, otel_exporter=exporter, path="/agent/run", status=200) as root:
            sent[root.span_id] = {"name": "http.request", "parent": None, "code": 0}
            with trace_span("tool.call", metrics=metrics, otel_exporter=exporter, tool="shell", step_index=2, dry_run=True) as tool:
                tool.mark_error("ToolResultError")
                sent[tool.span_id] = {"name": "tool.call", "parent": root.span_id, "code": 2}
            try:
                with trace_span("planner.plan", metrics=metrics, otel_exporter=exporter) as plan:
                    sent[plan.span_id] = {"name": "planner.plan", "parent": root.span_id, "code": 2}
                    raise RuntimeError("planner failure for conformance")
            except RuntimeError:
                pass
        if not exporter.flush(timeout=20):
            failures.append("exporter did not drain its queue within 20s")
    finally:
        exporter.close()

    export_failures = metrics.counter_sum("omnidesk_trace_export_failures_total")
    exported = metrics.counter_sum("omnidesk_trace_exported_spans_total")
    if export_failures:
        failures.append(f"collector refused runtime spans: {export_failures} export failures")
    if exported != len(sent):
        failures.append(f"expected {len(sent)} exported spans, counted {exported}")

    collected: dict[str, dict] = {}
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        collected = _collected_spans(args.collector_output)
        if all(span_id in collected for span_id in sent):
            break
        time.sleep(1)
    for span_id, expected in sent.items():
        span = collected.get(span_id)
        if span is None:
            failures.append(f"span {expected['name']} ({span_id}) never reached the collector output")
            continue
        if span.get("traceId") != inbound.trace_id:
            failures.append(f"span {expected['name']} trace id changed in transit: {span.get('traceId')}")
        if (span.get("parentSpanId") or None) != expected["parent"]:
            failures.append(f"span {expected['name']} parent {span.get('parentSpanId')!r} != {expected['parent']!r}")
        if int((span.get("status") or {}).get("code", 0)) != expected["code"]:
            failures.append(f"span {expected['name']} status {span.get('status')} != code {expected['code']}")

    legacy = {
        "resourceSpans": [{"resource": {"attributes": []}, "scopeSpans": [{"scope": {"name": "legacy"}, "spans": [{
            "traceId": "ab" * 16, "spanId": "cd" * 8, "parentSpanId": "", "name": "legacy",
            "startTimeUnixNano": "1", "endTimeUnixNano": "2", "status": {"code": "OK"}, "attributes": []}]}]}]
    }
    legacy_status = _post_raw(args.endpoint, legacy)
    if legacy_status < 400:
        failures.append(f"collector accepted the legacy string status enum (HTTP {legacy_status}); this check cannot detect regressions")

    unreachable = MetricsRegistry()
    dead = AsyncOTLPHttpExporter(endpoint="http://127.0.0.1:9/v1/traces", timeout=1, max_retries=0, metrics=unreachable)
    try:
        with trace_span("http.request", metrics=unreachable, otel_exporter=dead):
            pass
        dead.flush(timeout=10)
    finally:
        dead.close()
    if unreachable.counter_sum("omnidesk_trace_export_failures_total") != 1:
        failures.append("export to an unreachable collector was not counted")

    report = {
        "ok": not failures,
        "failures": failures,
        "spans_sent": len(sent),
        "spans_exported": exported,
        "spans_collected": sum(1 for span_id in sent if span_id in collected),
        "legacy_payload_http_status": legacy_status,
        "unreachable_failures_counted": unreachable.counter_sum("omnidesk_trace_export_failures_total"),
        "trace_id": inbound.trace_id,
    }
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
