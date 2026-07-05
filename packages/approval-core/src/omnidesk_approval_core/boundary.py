from __future__ import annotations

BOUNDARY_NAME = "approval-core"
OWNED_SOURCE_PATHS = (
    "omnidesk_agent/security",
    "omnidesk_agent/appsync",
    "omnidesk_agent/self_learning/approval.py",
)
FORBIDDEN_IMPORT_PREFIXES = (
    "apps.",
    "release.",
    "omnidesk_agent.channels",
)
