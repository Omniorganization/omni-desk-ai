"""Dependency-free OTLP/HTTP JSON trace exporter.

The wire format follows the OTLP/JSON encoding of
``opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest``:

* ``traceId`` / ``spanId`` / ``parentSpanId`` are lowercase hex strings
  (32 / 16 / 16 characters), never all zeros;
* enum fields (``status.code``, ``kind``) are integers;
* 64-bit integers (timestamps, ``intValue``) are decimal strings;
* attribute values are typed ``AnyValue`` objects.

A real OpenTelemetry Collector rejects payloads that violate these rules, so
``tests/test_observability_otlp_conformance.py`` checks them field by field and
the ``Observability Conformance`` workflow sends spans to a real collector.

Export failures are never silent: every failed, rejected or dropped span
increments a labelled counter on the caller's ``MetricsRegistry``.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol

_TRACEPARENT_VERSION = "00"
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")

# opentelemetry.proto.trace.v1.Status.StatusCode
STATUS_CODE_UNSET = 0
STATUS_CODE_OK = 1
STATUS_CODE_ERROR = 2
_STATUS_CODES = {"UNSET": STATUS_CODE_UNSET, "OK": STATUS_CODE_OK, "ERROR": STATUS_CODE_ERROR}

# opentelemetry.proto.trace.v1.Span.SpanKind
SPAN_KIND_INTERNAL = 1
SPAN_KIND_SERVER = 2
SPAN_KIND_CLIENT = 3
_SPAN_KINDS = {"INTERNAL": SPAN_KIND_INTERNAL, "SERVER": SPAN_KIND_SERVER, "CLIENT": SPAN_KIND_CLIENT}

# Collector responses that OTLP/HTTP says the client may retry.
_RETRYABLE_HTTP_STATUS = {429, 502, 503, 504}


class _Counter(Protocol):
    def inc(self, name: str, value: float = 1, **labels: Any) -> None: ...


def _valid_hex_id(value: str, length: int) -> bool:
    pattern = _HEX32 if length == 32 else _HEX16
    return bool(pattern.match(value)) and value != "0" * length


def normalize_trace_id(value: str | None) -> str:
    """Return a valid OTLP trace id for ``value``.

    UUIDs and 32-hex strings are kept (lowercased, dashes removed). Any other
    non-empty value is hashed, so the same request id always maps to the same
    valid trace id. An empty value yields a fresh random id.
    """
    if not value:
        return os.urandom(16).hex()
    compact = value.strip().lower().replace("-", "")
    if _valid_hex_id(compact, 32):
        return compact
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:32]


def normalize_span_id(value: str | None) -> str:
    if not value:
        return os.urandom(8).hex()
    compact = value.strip().lower().replace("-", "")
    if _valid_hex_id(compact, 16):
        return compact
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]


def make_traceparent(trace_id: str, span_id: str, sampled: bool = True) -> str:
    flags = "01" if sampled else "00"
    return f"{_TRACEPARENT_VERSION}-{normalize_trace_id(trace_id)}-{normalize_span_id(span_id)}-{flags}"


def parse_traceparent(value: str | None) -> dict[str, str] | None:
    """Parse a W3C ``traceparent`` header; return None for anything invalid."""
    if not value:
        return None
    parts = value.strip().lower().split("-")
    if len(parts) != 4 or parts[0] != _TRACEPARENT_VERSION:
        return None
    trace_id, parent_span_id, flags = parts[1], parts[2], parts[3]
    if not _valid_hex_id(trace_id, 32) or not _valid_hex_id(parent_span_id, 16):
        return None
    if not re.fullmatch(r"[0-9a-f]{2}", flags):
        return None
    return {"trace_id": trace_id, "parent_span_id": parent_span_id, "flags": flags}


def inject_traceparent(headers: dict[str, str], trace_id: str, span_id: str, sampled: bool = True) -> dict[str, str]:
    headers["traceparent"] = make_traceparent(trace_id, span_id, sampled=sampled)
    return headers


def _any_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _attributes(values: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{"key": str(key), "value": _any_value(val)} for key, val in sorted(values.items()) if val is not None]


@dataclass(frozen=True)
class OTelSpanRecord:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    start_unix_nano: int = 0
    end_unix_nano: int = 0
    status_code: str = "UNSET"
    attributes: dict[str, Any] = field(default_factory=dict)
    kind: str = "INTERNAL"
    status_message: str = ""

    def to_otlp_span(self) -> dict[str, Any]:
        status_code = _STATUS_CODES.get(str(self.status_code).upper(), STATUS_CODE_UNSET)
        status: dict[str, Any] = {"code": status_code}
        if status_code == STATUS_CODE_ERROR and self.status_message:
            status["message"] = self.status_message
        span: dict[str, Any] = {
            "traceId": normalize_trace_id(self.trace_id),
            "spanId": normalize_span_id(self.span_id),
            "name": self.name,
            "kind": _SPAN_KINDS.get(str(self.kind).upper(), SPAN_KIND_INTERNAL),
            "startTimeUnixNano": str(int(self.start_unix_nano)),
            "endTimeUnixNano": str(max(int(self.end_unix_nano), int(self.start_unix_nano))),
            "attributes": _attributes(self.attributes),
            "status": status,
        }
        if self.parent_span_id:
            span["parentSpanId"] = normalize_span_id(self.parent_span_id)
        return span

    def to_otlp_json(self, *, service_name: str = "omnidesk-agent") -> dict[str, Any]:
        return export_request_json([self], service_name=service_name)


def _resource_attributes(service_name: str) -> dict[str, Any]:
    attributes: dict[str, Any] = {"service.name": service_name}
    version = os.getenv("OMNIDESK_VERSION")
    environment = os.getenv("OMNIDESK_ENV")
    if version:
        attributes["service.version"] = version
    if environment:
        attributes["deployment.environment.name"] = environment
    return attributes


def export_request_json(spans: Iterable[OTelSpanRecord], *, service_name: str = "omnidesk-agent") -> dict[str, Any]:
    """Build one ``ExportTraceServiceRequest`` for a batch of spans."""
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _attributes(_resource_attributes(service_name))},
                "scopeSpans": [
                    {
                        "scope": {"name": "omnidesk_agent.observability_otel"},
                        "spans": [span.to_otlp_span() for span in spans],
                    }
                ],
            }
        ]
    }


class OTLPExportError(RuntimeError):
    """An export the collector did not accept; ``reason`` is a bounded label."""

    def __init__(self, reason: str, detail: str = "", *, retryable: bool = False, rejected: int | None = None):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.retryable = retryable
        self.rejected = rejected


def _classify_url_error(exc: BaseException) -> OTLPExportError:
    if isinstance(exc, urllib.error.HTTPError):
        status = int(exc.code)
        reason = "http_4xx" if 400 <= status < 500 else "http_5xx"
        return OTLPExportError(reason, f"HTTP {status}", retryable=status in _RETRYABLE_HTTP_STATUS)
    if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
        return OTLPExportError("timeout", str(exc), retryable=True)
    return OTLPExportError("connection", type(exc).__name__, retryable=True)


class OTLPHttpExporter:
    """Synchronous OTLP/HTTP JSON exporter (used by probes and drills)."""

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        timeout: float = 2.0,
        service_name: str = "omnidesk-agent",
        metrics: _Counter | None = None,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.2,
    ):
        self.endpoint = endpoint or os.getenv("OMNIDESK_OTLP_ENDPOINT", "")
        self.timeout = timeout
        self.service_name = service_name
        self.metrics = metrics
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))

    def enabled(self) -> bool:
        return bool(self.endpoint)

    def _validated_endpoint(self) -> str:
        parsed = urllib.parse.urlparse(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("OTLP endpoint must be an absolute http(s) URL")
        return self.endpoint

    def _post_once(self, payload: bytes, span_count: int) -> None:
        req = urllib.request.Request(
            self._validated_endpoint(),
            data=payload,
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            # Endpoint scheme and host are validated above.
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # nosec B310
                status = int(resp.status)
                body = resp.read(65536)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise _classify_url_error(exc) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise _classify_url_error(getattr(exc, "reason", exc) if isinstance(exc, urllib.error.URLError) else exc) from exc
        if not 200 <= status < 300:
            raise OTLPExportError("http_4xx" if status < 500 else "http_5xx", f"HTTP {status}")
        rejected = _partial_success_rejected(body)
        if rejected:
            raise OTLPExportError("rejected", f"collector rejected {rejected} of {span_count} spans", rejected=min(rejected, span_count))

    def export_batch(self, spans: list[OTelSpanRecord]) -> None:
        """POST one batch; raise OTLPExportError when it is not fully accepted."""
        if not spans:
            return
        payload = json.dumps(export_request_json(spans, service_name=self.service_name), separators=(",", ":")).encode("utf-8")
        attempt = 0
        while True:
            try:
                self._post_once(payload, len(spans))
                return
            except OTLPExportError as exc:
                if not exc.retryable or attempt >= self.max_retries:
                    raise
            attempt += 1
            time.sleep(self.retry_backoff_seconds * (2 ** (attempt - 1)))

    def export(self, span: OTelSpanRecord, *, metrics: _Counter | None = None) -> bool:
        if not self.enabled():
            return False
        registry = metrics or self.metrics
        try:
            self.export_batch([span])
        except OTLPExportError as exc:
            _count_failure(registry, exc.reason, exc.rejected or 1)
            raise
        except Exception:
            _count_failure(registry, "invalid", 1)
            raise
        if registry is not None:
            registry.inc("omnidesk_trace_exported_spans_total", 1)
        return True


def _partial_success_rejected(body: bytes) -> int:
    if not body:
        return 0
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return 0
    partial = decoded.get("partialSuccess") if isinstance(decoded, dict) else None
    if not isinstance(partial, dict):
        return 0
    try:
        return max(0, int(partial.get("rejectedSpans") or 0))
    except (TypeError, ValueError):
        return 0


def _count_failure(metrics: _Counter | None, reason: str, spans: int) -> None:
    if metrics is not None:
        metrics.inc("omnidesk_trace_export_failures_total", spans, reason=reason)


class AsyncOTLPHttpExporter(OTLPHttpExporter):
    """Non-blocking OTLP exporter with a bounded in-process queue.

    Request paths enqueue spans; a daemon worker sends them in batches. When
    the queue is full the oldest span is dropped instead of blocking traffic.
    Failed, rejected and dropped spans are counted on the registry passed to
    ``export`` (or the constructor), so ``/admin/metrics`` shows them:

    * ``omnidesk_trace_export_failures_total{reason}``
    * ``omnidesk_trace_export_dropped_total{reason}``
    * ``omnidesk_trace_exported_spans_total``
    """

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        timeout: float = 2.0,
        service_name: str = "omnidesk-agent",
        max_queue_size: int = 1000,
        max_batch_size: int = 128,
        metrics: _Counter | None = None,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.2,
    ):
        super().__init__(
            endpoint=endpoint,
            timeout=timeout,
            service_name=service_name,
            metrics=metrics,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
        )
        self.max_batch_size = max(1, int(max_batch_size))
        self._queue: queue.Queue[tuple[OTelSpanRecord, _Counter | None] | None] = queue.Queue(maxsize=max(1, int(max_queue_size)))
        self._pending = 0
        self._pending_changed = threading.Condition()
        self._closed = False
        self._worker: threading.Thread | None = None
        if self.enabled():
            self._worker = threading.Thread(target=self._drain, name="omnidesk-otlp-exporter", daemon=True)
            self._worker.start()

    def export(self, span: OTelSpanRecord, *, metrics: _Counter | None = None) -> bool:
        registry = metrics or self.metrics
        if not self.enabled():
            return False
        if self._closed:
            if registry is not None:
                registry.inc("omnidesk_trace_export_dropped_total", 1, reason="closed")
            return False
        item = (span, registry)
        if self._enqueue(item):
            return True
        try:
            dropped = self._queue.get_nowait()
        except queue.Empty:
            dropped = None
        if dropped is not None:
            self._settle(1)
            _, dropped_registry = dropped
            if dropped_registry is not None:
                dropped_registry.inc("omnidesk_trace_export_dropped_total", 1, reason="queue_full")
        if self._enqueue(item):
            return True
        if registry is not None:
            registry.inc("omnidesk_trace_export_dropped_total", 1, reason="queue_full")
        return False

    def _enqueue(self, item: tuple[OTelSpanRecord, _Counter | None]) -> bool:
        with self._pending_changed:
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                return False
            self._pending += 1
            return True

    def _settle(self, count: int) -> None:
        with self._pending_changed:
            self._pending = max(0, self._pending - count)
            self._pending_changed.notify_all()

    def _next_batch(self) -> tuple[list[tuple[OTelSpanRecord, _Counter | None]], bool]:
        first = self._queue.get()
        if first is None:
            return [], True
        batch = [first]
        stop = False
        while len(batch) < self.max_batch_size:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                stop = True
                break
            batch.append(item)
        return batch, stop

    def _drain(self) -> None:
        while True:
            batch, stop = self._next_batch()
            if batch:
                self._send(batch)
            if stop:
                return

    def _send(self, batch: list[tuple[OTelSpanRecord, _Counter | None]]) -> None:
        spans = [span for span, _ in batch]
        registries: dict[int, tuple[_Counter, int]] = {}
        for _, registry in batch:
            if registry is not None:
                current = registries.get(id(registry), (registry, 0))
                registries[id(registry)] = (registry, current[1] + 1)
        try:
            try:
                self.export_batch(spans)
            except OTLPExportError as exc:
                for registry, count in registries.values():
                    failed = count if exc.rejected is None else max(1, round(exc.rejected * count / len(spans)))
                    _count_failure(registry, exc.reason, failed)
                return
            except Exception:  # pragma: no cover - defensive: never kill the worker
                for registry, count in registries.values():
                    _count_failure(registry, "invalid", count)
                return
            for registry, count in registries.values():
                registry.inc("omnidesk_trace_exported_spans_total", count)
        finally:
            # Counted before settling, so flush() never returns ahead of the metrics.
            self._settle(len(batch))

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until every queued span was sent or counted as failed."""
        deadline = time.monotonic() + timeout
        with self._pending_changed:
            while self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._pending_changed.wait(remaining)
        return True

    def close(self, timeout: float = 1.0) -> None:
        self._closed = True
        if self._worker is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._worker.join(timeout=timeout)


def span_record_from_context(
    name: str,
    trace_id: str,
    span_id: str,
    parent_span_id: str | None,
    started: float,
    ended: float,
    status_code: str = "UNSET",
    attributes: Mapping[str, Any] | None = None,
    *,
    kind: str = "INTERNAL",
    status_message: str = "",
) -> OTelSpanRecord:
    return OTelSpanRecord(
        name=name,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        start_unix_nano=int(started * 1_000_000_000),
        end_unix_nano=int(ended * 1_000_000_000),
        status_code=status_code,
        attributes=dict(attributes or {}),
        kind=kind,
        status_message=status_message,
    )
