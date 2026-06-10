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
            row = con.execute("SELECT count FROM webhook_rate WHERE key=? AND bucket=?", (key, bucket)).fetchone()
            count = int(row[0]) if row else 0
            if count >= self.cfg.rate_limit_max_requests:
                raise PermissionError(f"webhook rate limit exceeded for {key}")
            if row:
                con.execute("UPDATE webhook_rate SET count=? WHERE key=? AND bucket=?", (count + 1, key, bucket))
            else:
                con.execute("INSERT INTO webhook_rate (key,bucket,count) VALUES (?,?,?)", (key, bucket, 1))

    def _mark_seen(self, channel: str, digest: str) -> bool:
        cutoff = time.time() - self.cfg.replay_ttl_seconds
        with connect_sqlite(self.db_path) as con:
            con.execute("DELETE FROM webhook_seen WHERE created_at < ?", (cutoff,))
            try:
                con.execute("INSERT INTO webhook_seen (digest, channel, created_at) VALUES (?, ?, ?)", (digest, channel, time.time()))
                return True
            except sqlite3.IntegrityError:
                return False

    @staticmethod
    def _digest(channel: str, body: bytes, message_id: Optional[str]) -> str:
        seed = (message_id.encode("utf-8") if message_id else body)
        return hashlib.sha256(channel.encode("utf-8") + b":" + seed).hexdigest()
