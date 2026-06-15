from __future__ import annotations

import json
import os
import queue
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

_TRACEPARENT_VERSION = "00"


def make_traceparent(trace_id: str, span_id: str, sampled: bool = True) -> str:
    tid = trace_id.replace("-", "")[:32].ljust(32, "0")
    sid = span_id.replace("-", "")[:16].ljust(16, "0")
    flags = "01" if sampled else "00"
    return f"{_TRACEPARENT_VERSION}-{tid}-{sid}-{flags}"


def parse_traceparent(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    parts = value.strip().split("-")
    if len(parts) != 4 or parts[0] != _TRACEPARENT_VERSION:
        return None
    trace_id, parent_span_id, flags = parts[1], parts[2], parts[3]
    if len(trace_id) != 32 or len(parent_span_id) != 16 or len(flags) != 2:
        return None
    return {"trace_id": trace_id, "parent_span_id": parent_span_id, "flags": flags}


def inject_traceparent(headers: dict[str, str], trace_id: str, span_id: str, sampled: bool = True) -> dict[str, str]:
    headers["traceparent"] = make_traceparent(trace_id, span_id, sampled=sampled)
    return headers


@dataclass(frozen=True)
class OTelSpanRecord:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    start_unix_nano: int = 0
    end_unix_nano: int = 0
    status_code: str = "OK"
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_otlp_json(self, *, service_name: str = "omnidesk-agent") -> dict[str, Any]:
        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": service_name}},
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "omnidesk_agent.observability_otel"},
                            "spans": [
                                {
                                    "traceId": self.trace_id.replace("-", "")[:32].ljust(32, "0"),
                                    "spanId": self.span_id.replace("-", "")[:16].ljust(16, "0"),
                                    "parentSpanId": (self.parent_span_id or "").replace("-", "")[:16],
                                    "name": self.name,
                                    "startTimeUnixNano": str(self.start_unix_nano),
                                    "endTimeUnixNano": str(self.end_unix_nano),
                                    "status": {"code": self.status_code},
                                    "attributes": [
                                        {"key": str(k), "value": {"stringValue": str(v)}}
                                        for k, v in sorted(self.attributes.items())
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }


class OTLPHttpExporter:
    """Small dependency-free OTLP/HTTP JSON exporter.

    The exporter is intentionally optional and fail-closed at the call site: if
    no endpoint is configured, spans remain available in JSON logs/metrics only.
    """

    def __init__(self, endpoint: str | None = None, *, timeout: float = 2.0, service_name: str = "omnidesk-agent"):
        self.endpoint = endpoint or os.getenv("OMNIDESK_OTLP_ENDPOINT", "")
        self.timeout = timeout
        self.service_name = service_name

    def enabled(self) -> bool:
        return bool(self.endpoint)

    def export(self, span: OTelSpanRecord) -> bool:
        if not self.enabled():
            return False
        parsed = urllib.parse.urlparse(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("OTLP endpoint must be an absolute http(s) URL")
        payload = json.dumps(span.to_otlp_json(service_name=self.service_name)).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={"content-type": "application/json"},
            method="POST",
        )
        # Endpoint scheme and host are validated above.
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # nosec B310
            return 200 <= int(resp.status) < 300



class AsyncOTLPHttpExporter(OTLPHttpExporter):
    """Non-blocking OTLP exporter with bounded in-process queue.

    Request paths enqueue spans and a daemon worker performs the HTTP export.
    Queue overflow drops the oldest telemetry rather than blocking production
    traffic; failures remain observable through caller-side metrics.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        timeout: float = 2.0,
        service_name: str = "omnidesk-agent",
        max_queue_size: int = 1000,
    ):
        super().__init__(endpoint=endpoint, timeout=timeout, service_name=service_name)
        self._queue: queue.Queue[OTelSpanRecord | None] = queue.Queue(maxsize=max(1, int(max_queue_size)))
        self._closed = False
        self._worker: threading.Thread | None = None
        if self.enabled():
            self._worker = threading.Thread(target=self._drain, name="omnidesk-otlp-exporter", daemon=True)
            self._worker.start()

    def export(self, span: OTelSpanRecord) -> bool:
        if not self.enabled() or self._closed:
            return False
        try:
            self._queue.put_nowait(span)
            return True
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(span)
                return True
            except queue.Full:
                return False

    def _drain(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                super().export(item)
            except Exception:
                # Export failures must never break request handling.
                pass

    def close(self, timeout: float = 1.0) -> None:
        self._closed = True
        if self._worker is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._worker.join(timeout=timeout)


def span_record_from_context(name: str, trace_id: str, span_id: str, parent_span_id: str | None, started: float, ended: float, status_code: str = "OK", attributes: Mapping[str, Any] | None = None) -> OTelSpanRecord:
    return OTelSpanRecord(
        name=name,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        start_unix_nano=int(started * 1_000_000_000),
        end_unix_nano=int(ended * 1_000_000_000),
        status_code=status_code,
        attributes=dict(attributes or {}),
    )
