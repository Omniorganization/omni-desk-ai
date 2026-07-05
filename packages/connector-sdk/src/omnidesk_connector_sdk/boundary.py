from __future__ import annotations

BOUNDARY_NAME = "connector-sdk"
OWNED_SOURCE_PATHS = (
    "omnidesk_agent/channels",
    "omnidesk_agent/integrations",
    "apps/shared/omni-app-api.contract.json",
)
FORBIDDEN_IMPORT_PREFIXES = (
    "omnidesk_agent.self_learning",
    "omnidesk_agent.self_upgrade",
    "apps.",
    "release.",
)
