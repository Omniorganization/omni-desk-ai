from __future__ import annotations

BOUNDARY_NAME = "agent-runtime"
OWNED_SOURCE_PATHS = (
    "omnidesk_agent/core",
    "omnidesk_agent/server.py",
    "omnidesk_agent/appsync",
)
FORBIDDEN_IMPORT_PREFIXES = (
    "apps.",
    "release.",
    "scripts.",
)
