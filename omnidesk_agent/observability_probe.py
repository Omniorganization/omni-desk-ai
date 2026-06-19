from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from omnidesk_agent.observability_otel import OTLPHttpExporter, span_record_from_context


@dataclass(frozen=True)
class OTelProbeResult:
    ok: bool
    endpoint: str
    latency_seconds: float
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "endpoint": self.endpoint, "latency_seconds": self.latency_seconds, "error": self.error}


def probe_otlp_endpoint(endpoint: str, *, timeout: float = 2.0, service_name: str = "omnidesk-agent-probe") -> OTelProbeResult:
    """Send one synthetic OTLP span to verify collector reachability.

    This deliberately uses the same dependency-free exporter as the runtime so
    staging/prod drills validate the exact wire path used by request traces.
    """

    started = time.time()
    exporter = OTLPHttpExporter(endpoint=endpoint, timeout=timeout, service_name=service_name)
    try:
        span = span_record_from_context(
            "production_closure.otlp_probe",
            "f" * 32,
            "e" * 16,
            None,
            started,
            time.time(),
            "OK",
            {"probe": "production_closure"},
        )
        ok = exporter.export(span)
        return OTelProbeResult(bool(ok), endpoint, time.time() - started, None if ok else "exporter disabled or collector returned non-2xx")
    except Exception as exc:
        return OTelProbeResult(False, endpoint, time.time() - started, str(exc))
