from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class AdminAuthDecision:
    ok: bool
    reason: str
    actor: str = "admin"


class AdminAuth:
    """Unified authentication gate for management APIs.

    Accepted credentials:
      - Authorization: Bearer <token>
      - X-OmniDesk-Admin-Token: <token>
      - X-OmniDesk-Gateway-Secret: <legacy shared secret>

    If no token is configured, local development calls from localhost are allowed
    only when `allow_local_without_token` is true.
    """

    def __init__(
        self,
        *,
        admin_token_env: str = "OMNIDESK_ADMIN_TOKEN",
        legacy_secret_env: Optional[str] = None,
        allow_local_without_token: bool = True,
    ):
        self.admin_token_env = admin_token_env
        self.legacy_secret_env = legacy_secret_env
        self.allow_local_without_token = allow_local_without_token

    def verify_headers(self, headers: Any, client_host: Optional[str] = None) -> AdminAuthDecision:
        expected = os.getenv(self.admin_token_env, "")
        legacy = os.getenv(self.legacy_secret_env, "") if self.legacy_secret_env else ""

        provided = ""
        auth = headers.get("authorization", "") if headers else ""
        if auth.lower().startswith("bearer "):
            provided = auth.split(" ", 1)[1].strip()
        provided = provided or (headers.get("x-omnidesk-admin-token", "") if headers else "")
        legacy_provided = headers.get("x-omnidesk-gateway-secret", "") if headers else ""

        if expected:
            if provided and hmac.compare_digest(provided, expected):
                return AdminAuthDecision(True, "admin token accepted")
            return AdminAuthDecision(False, "missing or invalid admin token")

        if legacy:
            if legacy_provided and hmac.compare_digest(legacy_provided, legacy):
                return AdminAuthDecision(True, "legacy gateway secret accepted")
            return AdminAuthDecision(False, "missing or invalid legacy gateway secret")

        if self.allow_local_without_token and client_host in {"127.0.0.1", "::1", "localhost", None}:
            return AdminAuthDecision(True, "local development without admin token")

        return AdminAuthDecision(False, "admin token is not configured")

    async def verify_request(self, request: Any) -> AdminAuthDecision:
        host = getattr(getattr(request, "client", None), "host", None)
        return self.verify_headers(request.headers, host)
