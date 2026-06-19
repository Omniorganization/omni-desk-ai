from __future__ import annotations

from omnidesk_agent.appsync.routes import register_appsync_routes
from omnidesk_agent.appsync.store import AppSyncStore

__all__ = ["AppSyncStore", "register_appsync_routes"]
