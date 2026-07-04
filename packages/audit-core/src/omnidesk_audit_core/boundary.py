from __future__ import annotations

BOUNDARY_NAME = "audit-core"
OWNED_SOURCE_PATHS = (
    "omnidesk_agent/observability.py",
    "omnidesk_agent/observability_tracing.py",
    "omnidesk_agent/observability_otel.py",
    "omnidesk_agent/self_learning/observability",
)
FORBIDDEN_IMPORT_PREFIXES = (
    "apps.",
    "release.",
    "omnidesk_agent.channels",
)
