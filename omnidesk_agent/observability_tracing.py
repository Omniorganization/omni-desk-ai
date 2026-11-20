from __future__ import annotations

import contextlib
import contextvars
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from .observability import JsonEventLogger, MetricsRegistry
from .observability_otel import (
    OTLPHttpExporter,
    normalize_span_id,
    normalize_trace_id,
    parse_traceparent,
    span_record_from_context,
)

_TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("omnidesk_trace_id", default=None)
_SPAN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("omnidesk_span_id", default=None)


def current_trace_id() -> str | None:
    return _TRACE_ID.get()


def ensure_trace_id(trace_id: str | None = None) -> str:
    """Return a valid 32-hex trace id: ``trace_id`` (normalized), the current one, or a new one."""
    current = _TRACE_ID.get()
    if trace_id:
        normalized = normalize_trace_id(trace_id)
        _TRACE_ID.set(normalized)
        return normalized
    if current:
        return current
    generated = normalize_trace_id(None)
    _TRACE_ID.set(generated)
    return generated


@dataclass(frozen=True)
class InboundTraceContext:
    trace_id: str
    parent_span_id: str | None
    sampled: bool


def trace_context_from_headers(request_id: str | None, traceparent: str | None) -> InboundTraceContext:
    """Derive the trace context of an inbound request.

    A valid W3C ``traceparent`` wins and its span becomes the parent of the
    request span. Otherwise the trace id is derived from the request id
    (kept when it is a UUID or 32-hex, hashed otherwise), so arbitrary client
    ``x-request-id`` values can never produce an invalid OTLP trace id.
    """
    parsed = parse_traceparent(traceparent)
    if parsed:
        return InboundTraceContext(parsed["trace_id"], parsed["parent_span_id"], parsed["flags"] == "01")
    return InboundTraceContext(normalize_trace_id(request_id), None, True)


@dataclass
class TraceContext:
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    error_type: str | None = None

    def mark_error(self, error_type: str) -> None:
        """Record a failure that was handled without raising (e.g. a tool error result)."""
        self.error_type = str(error_type or "error")[:64]


def _is_control_flow(exc: BaseException) -> bool:
    # A human-approval pause is the normal remote-approval path, not a failure.
    from .security.approval_required import ApprovalRequired

    return isinstance(exc, ApprovalRequired)


@contextlib.contextmanager
def trace_span(
    name: str,
    *,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
    kind: str = "INTERNAL",
    metrics: MetricsRegistry | None = None,
    logger: JsonEventLogger | None = None,
    otel_exporter: OTLPHttpExporter | None = None,
    **attributes: Any,
) -> Iterator[TraceContext]:
    """Open a span. ``parent_span_id`` (e.g. from an inbound traceparent) is used only for root spans."""
    current_parent = _SPAN_ID.get()
    parent = current_parent or (normalize_span_id(parent_span_id) if parent_span_id else None)
    tid = normalize_trace_id(trace_id) if trace_id else (_TRACE_ID.get() or normalize_trace_id(None))
    span_id = normalize_span_id(None)
    token_trace = _TRACE_ID.set(tid)
    token_span = _SPAN_ID.set(span_id)
    started = time.time()
    ctx = TraceContext(trace_id=tid, span_id=span_id, parent_span_id=parent, attributes=dict(attributes))
    outcome = "ok"
    if logger:
        logger.event("trace.span.start", trace_id=tid, span_id=span_id, parent_span_id=parent, span=name, **attributes)
    try:
        yield ctx
    except Exception as exc:
        if _is_control_flow(exc):
            outcome = "approval_required"
        else:
            ctx.error_type = type(exc).__name__
        raise
    finally:
        ended = time.time()
        duration = ended - started
        if ctx.error_type:
            outcome = "error"
            if metrics:
                metrics.inc("omnidesk_trace_span_failures_total", span=name, error_type=ctx.error_type)
            if logger:
                logger.event("trace.span.error", trace_id=tid, span_id=span_id, parent_span_id=parent, span=name, error_type=ctx.error_type)
        if metrics:
            metrics.inc("omnidesk_trace_spans_total", span=name)
            metrics.set("omnidesk_trace_span_duration_seconds", duration, span=name)
            metrics.observe("omnidesk_span_duration_seconds", duration, span=name, outcome=outcome)
        if otel_exporter and otel_exporter.enabled():
            status = "ERROR" if ctx.error_type else "UNSET"
            span_attributes = dict(attributes)
            if outcome != "ok":
                span_attributes["omnidesk.outcome"] = outcome
            record = span_record_from_context(
                name, tid, span_id, parent, started, ended, status, span_attributes,
                kind=kind, status_message=ctx.error_type or "",
            )
            try:
                otel_exporter.export(record, metrics=metrics)
            except Exception:
                # The exporter already counted the failure on ``metrics``; tracing never breaks the caller.
                pass
        if logger:
            logger.event("trace.span.end", trace_id=tid, span_id=span_id, parent_span_id=parent, span=name, duration_seconds=duration, outcome=outcome)
        _SPAN_ID.reset(token_span)
        _TRACE_ID.reset(token_trace)
