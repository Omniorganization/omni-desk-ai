"""POST /webhooks/gmail: Gmail push notifications through Cloud Pub/Sub.

The route is closed (404) unless Gmail is enabled and both
``channels.gmail.push_audience`` and ``push_service_account_email`` are set.
Every request must carry the push subscription's Google-signed OIDC token.
A notification only carries a history id; the new INBOX messages are read
with ``users.history.list`` and go through the sender allowlist into the job
queue. Handling is idempotent: the history cursor only advances after a
successful read and the job queue deduplicates Gmail message ids, so a
redelivered or replayed notification can only re-read history, never create
a second job. That is why the webhook digest store is not used here: marking
a notification seen before the read would turn Pub/Sub's retry of a failed
read (503) into a dropped notification.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request

from omnidesk_agent.channels.channel_auth import bearer_token
from omnidesk_agent.channels.google_oidc import GooglePushJwtVerifier
from omnidesk_agent.server_routes.webhook_guard import enqueue_webhook_message

MAX_PUSH_BODY_BYTES = 64 * 1024


def register_gmail_push_route(app: FastAPI, cfg: Any, rt: Any) -> None:
    gmail_cfg = cfg.channels.gmail
    verifier: list[GooglePushJwtVerifier] = []
    if gmail_cfg.enabled and gmail_cfg.push_audience and gmail_cfg.push_service_account_email:
        verifier.append(GooglePushJwtVerifier(gmail_cfg.push_audience, gmail_cfg.push_service_account_email))
        verifier[0].prefetch()

    @app.post("/webhooks/gmail")
    async def gmail_push(request: Request):
        adapter = rt.adapters.get("gmail")
        if not verifier or adapter is None:
            raise HTTPException(404, "Gmail push is not configured")
        try:
            verifier[0].verify(bearer_token(dict(request.headers)))
        except PermissionError as exc:
            metrics = getattr(rt, "metrics", None)
            if callable(getattr(metrics, "inc", None)):
                metrics.inc("omnidesk_webhook_signature_failures_total", channel="gmail", reason=str(exc)[:80])
            raise HTTPException(403, str(exc)) from exc
        body = await request.body()
        if len(body) > MAX_PUSH_BODY_BYTES:
            raise HTTPException(413, "Gmail push body too large")
        try:
            _address, history_id, _pubsub_message_id = adapter.parse_push(_json(body))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            messages = await adapter.fetch_new_messages(history_id)
        except Exception as exc:  # Gmail read failed: let Pub/Sub retry; the cursor did not move
            raise HTTPException(503, "Gmail history could not be read; the notification will be retried") from exc
        jobs = [enqueue_webhook_message(rt, message) for message in messages]
        return {"ok": True, "queued": len(jobs)}


def _json(body: bytes) -> Any:
    import json

    try:
        return json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Pub/Sub push body is not JSON") from exc
