"""POST /webhooks/gmail: Gmail push notifications through Cloud Pub/Sub.

The route is closed (404) unless Gmail is enabled and both
``channels.gmail.push_audience`` and ``push_service_account_email`` are set.
Every request must carry the push subscription's Google-signed OIDC token.
A notification only carries a history id; the new INBOX messages are read
with ``users.history.list`` and go through the sender allowlist into the job
queue. Handling is idempotent: the history cursor is saved only after every
message read for this notification is queued, and the job queue deduplicates
Gmail message ids, so a redelivered or replayed notification can only re-read
history, never create a second job, and an enqueue failure (500) is retried
by Pub/Sub with the cursor where it was. That is why the webhook digest store
is not used here: marking a notification seen before the read would turn
Pub/Sub's retry of a failed read (503) into a dropped notification.

A read that cannot succeed by retrying is not answered with 503: an expired
cursor (history.list 404) restarts from the notification's history id and the
gap is logged and counted, and a message deleted before it was read
(messages.get 404) is skipped. Every other read failure is logged with its
cause and counted before the 503.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from omnidesk_agent.channels.channel_auth import bearer_token
from omnidesk_agent.channels.google_oidc import GooglePushJwtVerifier
from omnidesk_agent.privacy.redaction import MemoryPrivacyFilter
from omnidesk_agent.server_routes.webhook_guard import enqueue_webhook_message

logger = logging.getLogger(__name__)
_REDACTOR = MemoryPrivacyFilter()

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
            batch = await adapter.fetch_new_messages(history_id)
        except Exception as exc:  # Gmail read failed: let Pub/Sub retry; the cursor did not move
            logger.warning(
                "Gmail push: reading history failed (%s: %s); answering 503 so Pub/Sub retries",
                type(exc).__name__, _REDACTOR.redact_text(str(exc))[:300],
            )
            _inc(rt, "omnidesk_gmail_push_read_failures_total", reason=type(exc).__name__)
            raise HTTPException(503, "Gmail history could not be read; the notification will be retried") from exc
        if batch.missing:
            logger.info("Gmail push: %d message(s) were deleted before they could be read and were skipped: %s", len(batch.missing), ", ".join(batch.missing[:20]))
            _inc(rt, "omnidesk_gmail_messages_missing_total", value=len(batch.missing))
        jobs = [enqueue_webhook_message(rt, message) for message in batch.messages]
        # Only now: an enqueue failure above raised (500) with the cursor
        # untouched, so Pub/Sub's redelivery reads these messages again.
        advanced = adapter.advance_history_id(batch.cursor)
        if batch.expired_cursor is not None and advanced:
            # Reported once, by the notification that resets the cursor (an
            # older redelivered notification cannot move it back).
            logger.warning(
                "Gmail push: stored history id %s has expired (history.list 404); INBOX mail received before history id %s "
                "was not queued (it stays in the mailbox); reading continues from there",
                batch.expired_cursor, batch.cursor,
            )
            _inc(rt, "omnidesk_gmail_history_gaps_total")
        created = sum(1 for job in jobs if job.get("created"))
        return {"ok": True, "queued": created, "duplicates": len(jobs) - created, "more": batch.more}


def _inc(rt: Any, name: str, value: float = 1, **labels: Any) -> None:
    inc = getattr(getattr(rt, "metrics", None), "inc", None)
    if callable(inc):
        inc(name, value, channel="gmail", **labels)


def _json(body: bytes) -> Any:
    import json

    try:
        return json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Pub/Sub push body is not JSON") from exc
