from __future__ import annotations

BOUNDARY_NAME = "audit-core"
OWNED_SOURCE_PATHS = (
    "omnidesk_agent/security/audit_worm.py",
    "release/production-evidence.manifest.json",
)
FORBIDDEN_IMPORT_PREFIXES = (
    "apps.",
    "release.",
    "omnidesk_agent.channels",
)
