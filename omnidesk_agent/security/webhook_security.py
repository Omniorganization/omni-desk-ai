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


@dataclass
class WebhookSecurityConfig:
    replay_ttl_seconds: int = 300
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
    ) -> dict[str, Any]:
        self._check_timestamp(timestamp)
        self._check_rate(channel, source_key)
        digest = self._digest(channel, body, message_id)
        inserted = self._mark_seen(channel, digest)
        if not inserted:
            raise PermissionError(f"duplicate webhook blocked: {channel}")
        return {"ok": True, "digest": digest}

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
        if timestamp is None:
            return
        if abs(time.time() - timestamp) > self.cfg.replay_ttl_seconds:
            raise PermissionError("webhook timestamp outside replay window")

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
                raise PermissionError(f"webhook rate limit exceeded for {key}")

    def _mark_seen(self, channel: str, digest: str) -> bool:
        cutoff = time.time() - max(self.cfg.replay_ttl_seconds, self.cfg.seen_retention_seconds)
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


def webhook_digest(channel: str, body: bytes, message_id: Optional[str]) -> str:
    """Replay digest for a delivery; message_id must be unique per channel, not per chat."""
    seed = (message_id.encode("utf-8") if message_id else body)
    return hashlib.sha256(channel.encode("utf-8") + b":" + seed).hexdigest()


def nonce_digest(channel: str, nonce: str) -> str:
    return hashlib.sha256(b"nonce:" + channel.encode("utf-8") + b":" + nonce.encode("utf-8")).hexdigest()
