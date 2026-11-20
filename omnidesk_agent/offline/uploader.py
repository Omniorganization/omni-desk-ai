"""Upload this node's offline outbox to its remote gateway's ``POST /app/sync``.

A node that ran offline (``runtime.offline_mode``) queues the records it created
in its local outbox. When the reconnect worker finds the network again, this
uploader sends the queued operations to the remote gateway, which applies them
under its conflict policy and answers per operation (applied, duplicate,
conflict held for review, or rejected); the worker marks each one accordingly.

Configuration (environment):

``OMNIDESK_SYNC_UPLOAD_URL``
    The remote gateway's base URL; defaults to ``gateway.public_base_url``.
    It must be ``https``; plain ``http`` is accepted only for a loopback host.
``OMNIDESK_SYNC_UPLOAD_TOKEN``
    Bearer token of the remote actor the operations belong to.
``OMNIDESK_SYNC_UPLOAD_ACTOR``
    That actor (default ``system``). Only this actor's queued operations are
    uploaded: one token must not publish other actors' records as its own.
``OMNIDESK_SYNC_CONFLICT_POLICY``
    ``manual-review`` (default), ``server-wins``, ``client-wins`` or ``merge``.

Without a URL and token the worker keeps reporting that no upload target is
configured, and the outbox stays queued.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

CONFLICT_POLICIES = ("manual-review", "server-wins", "client-wins", "merge")


class UploadRejected(RuntimeError):
    """The remote gateway refused the upload (an HTTP error status)."""


def _loopback(host: str) -> bool:
    if host in {"localhost"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_gateway_url(url: str) -> str:
    """The base URL, without a trailing slash; refuses credentials, queries and plain remote HTTP."""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise ValueError("sync upload URL must be an absolute http(s) URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("sync upload URL must not carry credentials, a query or a fragment")
    if parts.scheme == "http" and not _loopback(parts.hostname):
        raise ValueError("sync upload URL must use https for a non-loopback host; the token would travel in clear text")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


class GatewayOutboxUploader:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        actor: str = "system",
        conflict_policy: str = "manual-review",
        device_id: Optional[str] = None,
        timeout_seconds: float = 15.0,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        if not token:
            raise ValueError("sync upload token is required")
        if conflict_policy not in CONFLICT_POLICIES:
            raise ValueError(f"conflict_policy must be one of {', '.join(CONFLICT_POLICIES)}")
        self.base_url = validate_gateway_url(base_url)
        self._token = token
        self.actor = actor or "system"
        self.conflict_policy = conflict_policy
        self.device_id = device_id
        self.timeout_seconds = float(timeout_seconds)
        self._open = opener or urllib.request.urlopen

    def __call__(self, pending: list[dict[str, Any]]) -> dict[str, Any]:
        mine = [item for item in pending if str(item.get("actor") or "") == self.actor]
        others = [item for item in pending if str(item.get("actor") or "") != self.actor]
        operations = [
            {
                "operation_id": item.get("operation_id"),
                "operation_type": item.get("operation_type"),
                "idempotency_key": item.get("idempotency_key"),
                "payload": item.get("payload") or {},
                "created_at": item.get("created_at"),
            }
            for item in mine
        ]
        results: list[dict[str, Any]] = [
            {"operation_id": item.get("operation_id"), "status": "deferred", "reason": "no upload credentials for this actor"}
            for item in others
        ]
        if not operations:
            return {"ok": True, "operations": results}
        body = json.dumps(
            {"operations": operations, "device_id": self.device_id, "remote": "offline-outbox", "conflict_policy": self.conflict_policy},
            separators=(",", ":"),
        ).encode("utf-8")
        key = hashlib.sha256("|".join(str(item["operation_id"]) for item in operations).encode()).hexdigest()
        request = urllib.request.Request(
            self.base_url + "/app/sync",
            data=body,
            method="POST",
            headers={
                "authorization": f"Bearer {self._token}",
                "x-omnidesk-actor": self.actor,
                "content-type": "application/json",
                "idempotency-key": f"outbox-{key}",
            },
        )
        try:
            with self._open(request, timeout=self.timeout_seconds) as response:  # nosec B310 - validated https (or loopback) gateway URL
                answer = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raise UploadRejected(f"remote gateway answered HTTP {exc.code}") from exc
        uploaded = answer.get("uploaded") if isinstance(answer, dict) else None
        uploaded = uploaded if isinstance(uploaded, dict) else {}
        per_operation = uploaded.get("operations")
        if isinstance(per_operation, list):
            results.extend(item for item in per_operation if isinstance(item, dict))
        else:
            # A gateway that predates per-operation results: all delivered.
            results.extend({"operation_id": item["operation_id"], "status": "applied"} for item in operations)
        return {"ok": True, "remote_seq": answer.get("sync_seq") if isinstance(answer, dict) else None, "operations": results}


def build_outbox_uploader(cfg: Any) -> Optional[GatewayOutboxUploader]:
    """The configured uploader, or None when no remote gateway and token are set."""
    gateway = getattr(cfg, "gateway", None)
    url = os.getenv("OMNIDESK_SYNC_UPLOAD_URL", "").strip() or str(getattr(gateway, "public_base_url", "") or "").strip()
    token = os.getenv("OMNIDESK_SYNC_UPLOAD_TOKEN", "").strip()
    if not url or not token:
        return None
    return GatewayOutboxUploader(
        base_url=url,
        token=token,
        actor=os.getenv("OMNIDESK_SYNC_UPLOAD_ACTOR", "").strip() or "system",
        conflict_policy=os.getenv("OMNIDESK_SYNC_CONFLICT_POLICY", "").strip() or "manual-review",
    )
