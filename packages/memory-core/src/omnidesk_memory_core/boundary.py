from __future__ import annotations

BOUNDARY_NAME = "memory-core"
OWNED_SOURCE_PATHS = (
    "omnidesk_agent/memory",
    "omnidesk_agent/appsync/store.py",
    "omnidesk_agent/self_learning/store.py",
)
FORBIDDEN_IMPORT_PREFIXES = (
    "apps.",
    "release.",
    "omnidesk_agent.channels",
)
