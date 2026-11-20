from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from omnidesk_agent.storage.sqlite import connect_sqlite
from typing import Any, Optional

from omnidesk_agent.validation.webhook_signatures import line_signature_valid


# How far in the future an event timestamp may be (clock skew).
FUTURE_SKEW_SECONDS = 300


class WebhookDuplicate(PermissionError):
    """A delivery whose digest was already accepted; answer it, do not process it again."""


class WebhookRateLimited(PermissionError):
    """Too many deliveries from one source; the provider should retry after ``retry_after_seconds``."""

    def __init__(self, message: str, *, retry_after_seconds: int):
        super().__init__(message)
        self.retry_after_seconds = max(1, int(retry_after_seconds))


@dataclass
class WebhookSecurityConfig:
    # Oldest event timestamp accepted. Providers redeliver for hours to days
    # (Meta/WhatsApp up to 7 days) with the original event time, so the window
    # matches how long accepted digests are remembered: a replay inside it is
    # caught by the digest, anything older by this check. Providers that sign a
    # request timestamp (Slack) keep their own short window in the adapter.
    replay_ttl_seconds: int = 7 * 24 * 3600
    # How long seen message digests are kept. Many channels (DingTalk, X, Lark,
    # Discord, Teams, Matrix, Telegram) carry no signed timestamp, so a captured
    # signed request could be replayed once its digest expired; keep them longer
    # than any realistic capture-and-replay window.
    seen_retention_seconds: int = 7 * 24 * 3600
    rate_limit_window_seconds: int = 60
    rate_limit_max_requests: int = 60


class WebhookSecurity:
    """Unified replay protection, idempotency and rate limiting for webhooks."""

    def __init__(self, db_path: Path, cfg: Optional[WebhookSecurityConfig] = None):
        self.db_path = db_path.expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg = cfg or WebhookSecurityConfig()
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_seen (
                    digest TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_rate (
                    key TEXT NOT NULL,
                    bucket INTEGER NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY (key, bucket)
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_nonce (
                    digest TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )

    def guard(
        self,
        *,
        channel: str,
        body: bytes,
        source_key: str,
        message_id: Optional[str] = None,
        timestamp: Optional[float] = None,
        claim: bool = True,
    ) -> dict[str, Any]:
        """Timestamp, rate and replay checks for one verified delivery.

        With ``claim=False`` the digest is only checked; the caller records it
        with ``mark_seen`` once the delivery is durably enqueued, so a failed
        enqueue leaves the provider's redelivery acceptable.
        """
        self._check_timestamp(timestamp)
        self._check_rate(channel, source_key)
        digest = self._digest(channel, body, message_id)
        if not claim:
            if self.is_seen(digest):
                raise WebhookDuplicate(f"duplicate webhook blocked: {channel}")
            return {"ok": True, "digest": digest}
        inserted = self._mark_seen(channel, digest)
        if not inserted:
            raise WebhookDuplicate(f"duplicate webhook blocked: {channel}")
        return {"ok": True, "digest": digest}

    def is_seen(self, digest: str) -> bool:
        cutoff = time.time() - self._seen_retention_seconds()
        with connect_sqlite(self.db_path) as con:
            row = con.execute("SELECT 1 FROM webhook_seen WHERE digest = ? AND created_at >= ?", (digest, cutoff)).fetchone()
        return row is not None

    def mark_seen(self, channel: str, digest: str) -> bool:
        """Record an enqueued delivery; False when it was already recorded."""
        return self._mark_seen(channel, digest)

    def _seen_retention_seconds(self) -> float:
        return seen_retention_seconds(self.cfg)

    def claim_nonce(self, *, channel: str, nonce: str, ttl_seconds: float) -> None:
        """Consume a signed single-use nonce in the shared store.

        Process memory is not enough: every replica and every restart has to see
        the same claim, otherwise a captured signed query string can be replayed
        against another instance while the signature is still fresh.
        """
        now = time.time()
        digest = nonce_digest(channel, nonce)
        with connect_sqlite(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM webhook_nonce WHERE expires_at < ?", (now,))
            try:
                con.execute(
                    "INSERT INTO webhook_nonce (digest, channel, expires_at) VALUES (?, ?, ?)",
                    (digest, channel, now + max(1.0, float(ttl_seconds))),
                )
            except sqlite3.IntegrityError as exc:
                raise PermissionError(f"replayed webhook nonce blocked: {channel}") from exc

    def verify_line(self, body: bytes, channel_secret: str, signature: str) -> None:
        if not line_signature_valid(body, channel_secret, signature):
            raise PermissionError("invalid LINE webhook signature")

    def verify_hmac_sha256_hex(self, body: bytes, secret: str, signature: str, prefix: str = "") -> None:
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        candidate = signature[len(prefix):] if prefix and signature.startswith(prefix) else signature
        if not hmac.compare_digest(expected, candidate):
            raise PermissionError("invalid HMAC-SHA256 signature")

    def _check_timestamp(self, timestamp: Optional[float]) -> None:
        check_event_timestamp(self.cfg, timestamp)

    def _check_rate(self, channel: str, source_key: str) -> None:
        bucket = int(time.time() // self.cfg.rate_limit_window_seconds)
        key = f"{channel}:{source_key}"
        with connect_sqlite(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                """
                INSERT INTO webhook_rate (key, bucket, count) VALUES (?, ?, 1)
                ON CONFLICT(key, bucket) DO UPDATE SET count = count + 1
                """,
                (key, bucket),
            )
            row = con.execute("SELECT count FROM webhook_rate WHERE key=? AND bucket=?", (key, bucket)).fetchone()
            count = int(row[0]) if row else 0
            if count > self.cfg.rate_limit_max_requests:
                raise WebhookRateLimited(f"webhook rate limit exceeded for {key}", retry_after_seconds=rate_retry_after(self.cfg))

    def _mark_seen(self, channel: str, digest: str) -> bool:
        cutoff = time.time() - self._seen_retention_seconds()
        with connect_sqlite(self.db_path) as con:
            con.execute("DELETE FROM webhook_seen WHERE created_at < ?", (cutoff,))
            try:
                con.execute("INSERT INTO webhook_seen (digest, channel, created_at) VALUES (?, ?, ?)", (digest, channel, time.time()))
                return True
            except sqlite3.IntegrityError:
                return False

    @staticmethod
    def _digest(channel: str, body: bytes, message_id: Optional[str]) -> str:
        return webhook_digest(channel, body, message_id)


def seen_retention_seconds(cfg: WebhookSecurityConfig) -> float:
    return float(cfg.seen_retention_seconds)


def max_event_age_seconds(cfg: WebhookSecurityConfig) -> float:
    # Never accept an event older than the digest store remembers: a replay of
    # it would no longer be recognised.
    return float(min(cfg.replay_ttl_seconds, cfg.seen_retention_seconds))


def check_event_timestamp(cfg: WebhookSecurityConfig, timestamp: Optional[float]) -> None:
    """Refuse events from the future (beyond clock skew) or older than the replay window."""
    if timestamp is None:
        return
    now = time.time()
    if timestamp > now + FUTURE_SKEW_SECONDS or now - timestamp > max_event_age_seconds(cfg):
        raise PermissionError("webhook timestamp outside replay window")


def rate_retry_after(cfg: WebhookSecurityConfig, now: Optional[float] = None) -> int:
    """Seconds until the current fixed rate window ends."""
    window = max(1, int(cfg.rate_limit_window_seconds))
    now = time.time() if now is None else now
    return max(1, int(window - (now % window)) + 1)


def webhook_digest(channel: str, body: bytes, message_id: Optional[str]) -> str:
    """Replay digest for a delivery; message_id must be unique per channel, not per chat."""
    seed = (message_id.encode("utf-8") if message_id else body)
    return hashlib.sha256(channel.encode("utf-8") + b":" + seed).hexdigest()


def nonce_digest(channel: str, nonce: str) -> str:
    return hashlib.sha256(b"nonce:" + channel.encode("utf-8") + b":" + nonce.encode("utf-8")).hexdigest()
