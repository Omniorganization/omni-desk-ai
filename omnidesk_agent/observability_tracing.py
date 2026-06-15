from __future__ import annotations

import contextlib
import contextvars
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

from .observability import JsonEventLogger, MetricsRegistry
from .observability_otel import OTLPHttpExporter, span_record_from_context

_TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("omnidesk_trace_id", default=None)
_SPAN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("omnidesk_span_id", default=None)


def current_trace_id() -> str | None:
    return _TRACE_ID.get()


def ensure_trace_id(trace_id: str | None = None) -> str:
    current = _TRACE_ID.get()
    if trace_id:
        _TRACE_ID.set(trace_id)
        return trace_id
    if current:
        return current
    generated = str(uuid.uuid4())
    _TRACE_ID.set(generated)
    return generated


@dataclass
class TraceContext:
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@contextlib.contextmanager
def trace_span(
    name: str,
    *,
    trace_id: str | None = None,
    metrics: MetricsRegistry | None = None,
    logger: JsonEventLogger | None = None,
    otel_exporter: OTLPHttpExporter | None = None,
    **attributes: Any,
) -> Iterator[TraceContext]:
    parent_span_id = _SPAN_ID.get()
    tid = ensure_trace_id(trace_id)
    span_id = str(uuid.uuid4())
    token_trace = _TRACE_ID.set(tid)
    token_span = _SPAN_ID.set(span_id)
    started = time.time()
    ctx = TraceContext(trace_id=tid, span_id=span_id, parent_span_id=parent_span_id, attributes=dict(attributes))
    span_failed = False
    if logger:
        logger.event("trace.span.start", trace_id=tid, span_id=span_id, parent_span_id=parent_span_id, span=name, **attributes)
    try:
        yield ctx
    except Exception as exc:
        span_failed = True
        if metrics:
            metrics.inc("omnidesk_trace_span_failures_total", span=name, error_type=type(exc).__name__)
        if logger:
            logger.event("trace.span.error", trace_id=tid, span_id=span_id, parent_span_id=parent_span_id, span=name, error_type=type(exc).__name__)
        raise
    finally:
        duration = time.time() - started
        if metrics:
            metrics.inc("omnidesk_trace_spans_total", span=name)
            metrics.set("omnidesk_trace_span_duration_seconds", duration, span=name)
        if otel_exporter and otel_exporter.enabled():
            status = "ERROR" if span_failed else "OK"
            try:
                otel_exporter.export(span_record_from_context(name, tid, span_id, parent_span_id, started, time.time(), status, attributes))
            except Exception as exc:
                if metrics:
                    metrics.inc("omnidesk_trace_export_failures_total", span=name, error_type=type(exc).__name__)
        if logger:
            logger.event("trace.span.end", trace_id=tid, span_id=span_id, parent_span_id=parent_span_id, span=name, duration_seconds=duration)
        _SPAN_ID.reset(token_span)
        _TRACE_ID.reset(token_trace)
