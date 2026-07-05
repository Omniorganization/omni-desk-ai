from __future__ import annotations

BOUNDARY_NAME = "policy-engine"
OWNED_SOURCE_PATHS = (
    "omnidesk_agent/security",
    "omnidesk_agent/validation",
    "omnidesk_agent/self_learning/policy.py",
)
FORBIDDEN_IMPORT_PREFIXES = (
    "apps.",
    "release.",
    "omnidesk_agent.channels",
)
