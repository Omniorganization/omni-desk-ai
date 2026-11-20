"""OTLP export must be accepted by a real collector, and its failures must be visible.

Why: the previous exporter sent ``status.code`` as the string "OK", which the
OTLP/JSON encoding does not allow (enums are integers), derived trace ids from
arbitrary client request ids (non-hex), and swallowed every asynchronous
export error. Traces were silently lost and the export-failure alert could
never fire. These tests pin the wire rules a collector enforces and the
counters operators alert on.
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from omnidesk_agent.observability import MetricsRegistry, initialize_runtime_metrics
from omnidesk_agent.observability_otel import (
    STATUS_CODE_ERROR,
    AsyncOTLPHttpExporter,
    OTLPExportError,
    OTLPHttpExporter,
    export_request_json,
    make_traceparent,
    parse_traceparent,
    span_record_from_context,
)
from omnidesk_agent.observability_tracing import trace_context_from_headers, trace_span
from omnidesk_agent.security.approval_required import ApprovalRequired

HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX16 = re.compile(r"^[0-9a-f]{16}$")
UINT64_STRING = re.compile(r"^[0-9]+$")


def _spans(payload: dict) -> list[dict]:
    return [span for rs in payload["resourceSpans"] for ss in rs["scopeSpans"] for span in ss["spans"]]


def _assert_otlp_span(span: dict) -> None:
    assert HEX32.match(span["traceId"]) and span["traceId"] != "0" * 32
    assert HEX16.match(span["spanId"]) and span["spanId"] != "0" * 16
    if "parentSpanId" in span:
        assert HEX16.match(span["parentSpanId"])
    assert isinstance(span["status"]["code"], int) and span["status"]["code"] in {0, 1, 2}
    assert isinstance(span["kind"], int) and 0 <= span["kind"] <= 5
    assert UINT64_STRING.match(span["startTimeUnixNano"]) and UINT64_STRING.match(span["endTimeUnixNano"])
    assert int(span["endTimeUnixNano"]) >= int(span["startTimeUnixNano"])
    for attribute in span["attributes"]:
        (value_type,) = attribute["value"].keys()
        assert value_type in {"stringValue", "boolValue", "intValue", "doubleValue"}
        if value_type == "intValue":
            assert UINT64_STRING.match(attribute["value"]["intValue"].lstrip("-"))


class _Collector(BaseHTTPRequestHandler):
    """Fake collector that answers like OTLP/HTTP does for each scenario."""

    mode = "ok"
    bodies: list[dict] = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("content-length", "0")))
        self.__class__.bodies.append(json.loads(body))
        if self.mode == "bad_request":
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"code":3,"message":"invalid"}')
            return
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        if self.mode == "partial":
            self.wfile.write(json.dumps({"partialSuccess": {"rejectedSpans": 1, "errorMessage": "bad span"}}).encode())
        else:
            self.wfile.write(b"{}")

    def log_message(self, *args):
        return None


@pytest.fixture
def collector():
    _Collector.bodies = []
    _Collector.mode = "ok"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/traces"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_otlp_json_uses_integer_enums_hex_ids_and_typed_attributes() -> None:
    record = span_record_from_context(
        "tool.call", "rid1", "not-hex", None, 1.0, 2.5, "ERROR",
        {"tool": "shell", "step_index": 3, "dry_run": False, "cost_usd": 0.25},
        kind="SERVER", status_message="ToolCrashed",
    )
    payload = export_request_json([record])
    (span,) = _spans(payload)
    _assert_otlp_span(span)
    assert span["status"] == {"code": STATUS_CODE_ERROR, "message": "ToolCrashed"}
    assert span["kind"] == 2
    # Root spans carry no parentSpanId at all; an empty string is not a valid id.
    assert "parentSpanId" not in span
    values = {a["key"]: a["value"] for a in span["attributes"]}
    assert values["step_index"] == {"intValue": "3"}
    assert values["dry_run"] == {"boolValue": False}
    assert values["cost_usd"] == {"doubleValue": 0.25}
    resource = payload["resourceSpans"][0]["resource"]["attributes"]
    assert {"key": "service.name", "value": {"stringValue": "omnidesk-agent"}} in resource


def test_client_request_ids_cannot_produce_invalid_trace_ids() -> None:
    for request_id in ("rid1", "x" * 200, "../../etc", "", None, "ABCDEF0123456789ABCDEF0123456789"):
        ctx = trace_context_from_headers(request_id, None)
        assert HEX32.match(ctx.trace_id) and ctx.trace_id != "0" * 32
        assert ctx.parent_span_id is None
    # The same request id maps to the same trace id, so logs and traces correlate.
    assert trace_context_from_headers("rid1", None).trace_id == trace_context_from_headers("rid1", None).trace_id
    # A UUID request id is kept, only compacted.
    assert trace_context_from_headers("1b4e28ba-2fa1-11d2-883f-0016d3cca427", None).trace_id == "1b4e28ba2fa111d2883f0016d3cca427"


def test_inbound_traceparent_parent_span_is_propagated() -> None:
    inbound = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    ctx = trace_context_from_headers("ignored", inbound)
    assert ctx.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert ctx.parent_span_id == "00f067aa0ba902b7"
    with trace_span("http.request", trace_id=ctx.trace_id, parent_span_id=ctx.parent_span_id) as span:
        assert span.parent_span_id == "00f067aa0ba902b7"
        with trace_span("tool.call") as child:
            assert child.trace_id == ctx.trace_id
            assert child.parent_span_id == span.span_id
    header = make_traceparent(span.trace_id, span.span_id)
    assert parse_traceparent(header) == {"trace_id": ctx.trace_id, "parent_span_id": span.span_id, "flags": "01"}
    # All-zero ids are invalid per W3C and must not be adopted.
    assert parse_traceparent("00-" + "0" * 32 + "-00f067aa0ba902b7-01") is None


def test_async_export_to_unreachable_collector_is_counted() -> None:
    metrics = MetricsRegistry()
    exporter = AsyncOTLPHttpExporter(endpoint="http://127.0.0.1:1/v1/traces", timeout=0.2, max_retries=0, metrics=metrics)
    try:
        for _ in range(3):
            with trace_span("http.request", metrics=metrics, otel_exporter=exporter):
                pass
        assert exporter.flush(timeout=10)
    finally:
        exporter.close()
    assert metrics.counter_sum("omnidesk_trace_export_failures_total") == 3
    assert metrics.counter_sum("omnidesk_trace_export_failures_total", reason="connection") == 3
    assert metrics.counter_sum("omnidesk_trace_exported_spans_total") == 0


def test_collector_rejections_are_counted_by_reason(collector) -> None:
    metrics = MetricsRegistry()
    exporter = AsyncOTLPHttpExporter(endpoint=collector, timeout=2, max_retries=0, metrics=metrics)
    try:
        _Collector.mode = "bad_request"
        exporter.export(span_record_from_context("a", "a" * 32, "b" * 16, None, 1, 2))
        assert exporter.flush(timeout=10)
        _Collector.mode = "partial"
        exporter.export(span_record_from_context("b", "a" * 32, "c" * 16, None, 1, 2))
        assert exporter.flush(timeout=10)
        _Collector.mode = "ok"
        exporter.export(span_record_from_context("c", "a" * 32, "d" * 16, None, 1, 2))
        assert exporter.flush(timeout=10)
    finally:
        exporter.close()
    assert metrics.counter_sum("omnidesk_trace_export_failures_total", reason="http_4xx") == 1
    assert metrics.counter_sum("omnidesk_trace_export_failures_total", reason="rejected") == 1
    assert metrics.counter_sum("omnidesk_trace_exported_spans_total") == 1
    for body in _Collector.bodies:
        for span in _spans(body):
            _assert_otlp_span(span)


def test_sync_probe_exporter_raises_and_counts_rejection(collector) -> None:
    metrics = MetricsRegistry()
    _Collector.mode = "bad_request"
    exporter = OTLPHttpExporter(endpoint=collector, timeout=2, max_retries=0, metrics=metrics)
    with pytest.raises(OTLPExportError) as excinfo:
        exporter.export(span_record_from_context("probe", "a" * 32, "b" * 16, None, 1, 2))
    assert excinfo.value.reason == "http_4xx"
    assert metrics.counter_sum("omnidesk_trace_export_failures_total", reason="http_4xx") == 1


def test_queue_overflow_drops_oldest_and_counts_it() -> None:
    metrics = MetricsRegistry()
    exporter = AsyncOTLPHttpExporter(endpoint="http://127.0.0.1:1/v1/traces", timeout=0.2, max_queue_size=1, max_retries=0, metrics=metrics)
    # Hold the worker so the one-slot queue overflows while spans keep arriving.
    blocker = threading.Event()
    original_send = exporter._send

    def slow_send(batch):
        blocker.wait(5)
        original_send(batch)

    exporter._send = slow_send  # type: ignore[method-assign]
    try:
        for index in range(5):
            exporter.export(span_record_from_context(f"s{index}", "a" * 32, f"{index:016x}", None, 1, 2))
        dropped = metrics.counter_sum("omnidesk_trace_export_dropped_total", reason="queue_full")
        assert dropped >= 3
    finally:
        blocker.set()
        exporter.flush(timeout=10)
        exporter.close()
    accounted = (
        metrics.counter_sum("omnidesk_trace_export_dropped_total")
        + metrics.counter_sum("omnidesk_trace_export_failures_total")
        + metrics.counter_sum("omnidesk_trace_exported_spans_total")
    )
    # Every span is either sent, failed or dropped; nothing disappears uncounted.
    assert accounted == 5


def test_approval_pause_is_not_a_span_failure_but_handled_errors_are() -> None:
    metrics = MetricsRegistry()
    with pytest.raises(ApprovalRequired):
        with trace_span("tool.call", metrics=metrics):
            raise ApprovalRequired("approval-1", {"tool": "shell"})
    assert metrics.counter_sum("omnidesk_trace_span_failures_total") == 0
    with trace_span("tool.call", metrics=metrics) as span:
        span.mark_error("ToolResultError")
    assert metrics.counter_sum("omnidesk_trace_span_failures_total", span="tool.call", error_type="ToolResultError") == 1
    with pytest.raises(RuntimeError):
        with trace_span("tool.call", metrics=metrics):
            raise RuntimeError("boom")
    assert metrics.counter_sum("omnidesk_trace_span_failures_total", error_type="RuntimeError") == 1


def test_span_durations_are_histograms_for_latency_percentiles() -> None:
    metrics = MetricsRegistry()
    initialize_runtime_metrics(metrics)
    with trace_span("tool.call", metrics=metrics):
        pass
    rendered = metrics.render_prometheus()
    assert "# TYPE omnidesk_span_duration_seconds histogram" in rendered
    assert re.search(r'omnidesk_span_duration_seconds_bucket\{le="\+Inf",outcome="ok",span="tool\.call"\} 1', rendered)


def test_exposition_has_one_type_per_family_and_no_type_conflicts() -> None:
    metrics = MetricsRegistry(max_series=10_000)
    initialize_runtime_metrics(metrics)
    metrics.observe("omnidesk_http_request_duration_seconds", 0.2, path="/health", method="GET")
    metrics.inc("omnidesk_http_requests_total", method="GET", path="/health", status=200)
    metrics.set("omnidesk_http_last_latency_seconds", 0.2, path="/health")
    rendered = metrics.render_prometheus()
    type_lines = [line for line in rendered.splitlines() if line.startswith("# TYPE ")]
    families = [line.split()[2] for line in type_lines]
    assert len(families) == len(set(families)), "a family declared twice makes Prometheus reject the scrape"
    assert "# TYPE omnidesk_http_request_duration_seconds histogram" in rendered
    # The latency SLO and dashboard read these two series.
    assert 'omnidesk_http_request_duration_seconds_bucket{le="1.0",method="GET",path="/health"} 1' in rendered
    assert 'omnidesk_http_request_duration_seconds_count{method="GET",path="/health"} 1' in rendered
    # The histogram family must not also appear as a bare untyped/counter sample.
    assert not re.search(r"^omnidesk_http_request_duration_seconds \d", rendered, re.M)
    assert "# TYPE omnidesk_metrics_dropped_series_updates_total counter" in rendered


def test_dropped_series_are_exported_for_alerting() -> None:
    metrics = MetricsRegistry(max_series=1)
    metrics.inc("a_total")
    metrics.inc("b_total")
    rendered = metrics.render_prometheus()
    assert "omnidesk_metrics_dropped_series_updates_total 1.0" in rendered
