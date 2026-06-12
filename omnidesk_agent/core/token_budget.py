from __future__ import annotations
from typing import Optional

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from omnidesk_agent.storage.sqlite import connect_sqlite
from omnidesk_agent.storage.migrations import Migration, apply_migrations


@dataclass
class TokenBudgetConfig:
    # Token budget is now a guardrail, not a hard blocker for verified-required calls.
    max_input_chars: int = 12000
    max_output_tokens: int = 1200
    cache_ttl_seconds: int = 86400
    enable_cache: bool = True

    # Optional warning threshold. If a call is not verified-required, it can still be blocked.
    # If verified_required=True, this threshold is overridden and the call is allowed.
    require_approval_above_estimated_tokens: int = 20000

    # Per-task hard budget has been removed by design.
    # Kept only for backward config compatibility; it is not used as a hard limit.
    per_task_max_llm_calls: Optional[int] = None


@dataclass
class TokenDecision:
    allowed: bool
    reason: str
    estimated_input_tokens: int
    estimated_output_tokens: int
    cache_key: Optional[str] = None
    truncated_system: Optional[str] = None
    truncated_user: Optional[str] = None
    budget_overridden: bool = False
    verified_required: bool = False


class TokenBudgetManager:
    """Guardrail for avoiding unnecessary LLM calls.

    Design principle:
    - Do not waste tokens on repeated, unplanned, or unbounded calls.
    - Do not block a call that has already been verified as necessary.
    - If a verified-required call exceeds the budget threshold, allow it and record
      that the budget was overridden.
    """

    def __init__(self, db_path: Path, config: Optional[TokenBudgetConfig] = None):
        self.db_path = db_path.expanduser()
        self.config = config or TokenBudgetConfig()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_cache (
                    cache_key TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    response TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    estimated_input_tokens INTEGER NOT NULL,
                    estimated_output_tokens INTEGER NOT NULL,
                    verified_required INTEGER NOT NULL DEFAULT 0,
                    budget_overridden INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )
                """
            )
            apply_migrations(con, [Migration(1, "token_budget_schema_baseline", lambda _con: None)])

    @staticmethod
    def estimate_tokens(text: str) -> int:
        return max(1, len(text) // 3)

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def make_cache_key(self, model: str, system: str, user: str) -> str:
        payload = json.dumps(
            {"model": model, "system": self._hash(system), "user": self._hash(user)},
            ensure_ascii=False,
            sort_keys=True,
        )
        return self._hash(payload)

    @staticmethod
    def trim(text: str, limit: int, *, verified_required: bool = False) -> str:
        # Verified-required calls should still avoid waste, but not destroy required context.
        # Therefore we only trim when the text is far beyond the configured limit.
        if verified_required:
            hard_limit = max(limit * 3, limit)
            if len(text) <= hard_limit:
                return text
            half = max(1, hard_limit // 2)
            return (
                text[:half]
                + f"\n\n...[TRUNCATED {len(text) - hard_limit} CHARS AFTER REQUIRED-CALL VALIDATION]...\n\n"
                + text[-half:]
            )

        if len(text) <= limit:
            return text
        half = max(1, limit // 2)
        return (
            text[:half]
            + f"\n\n...[TRUNCATED {len(text) - limit} CHARS TO SAVE TOKENS]...\n\n"
            + text[-half:]
        )

    def decide(
        self,
        *,
        model: str,
        system: str,
        user: str,
        task_id: str = "default",
        expected_output_tokens: Optional[int] = None,
        verified_required: bool = False,
    ) -> TokenDecision:
        cfg = self.config
        expected_output_tokens = expected_output_tokens or cfg.max_output_tokens

        truncated_system = self.trim(
            system,
            cfg.max_input_chars // 3,
            verified_required=verified_required,
        )
        truncated_user = self.trim(
            user,
            cfg.max_input_chars,
            verified_required=verified_required,
        )

        estimated_input = self.estimate_tokens(truncated_system + truncated_user)
        total_estimated = estimated_input + expected_output_tokens
        cache_key = self.make_cache_key(model, truncated_system, truncated_user)

        if (
            total_estimated > cfg.require_approval_above_estimated_tokens
            and not verified_required
        ):
            return TokenDecision(
                allowed=False,
                reason=(
                    "estimated token use exceeds threshold and the call has not "
                    "been verified as required"
                ),
                estimated_input_tokens=estimated_input,
                estimated_output_tokens=expected_output_tokens,
                cache_key=cache_key,
                truncated_system=truncated_system,
                truncated_user=truncated_user,
                budget_overridden=False,
                verified_required=False,
            )

        budget_overridden = (
            total_estimated > cfg.require_approval_above_estimated_tokens
            and verified_required
        )

        return TokenDecision(
            allowed=True,
            reason=(
                "required token use verified; budget threshold overridden"
                if budget_overridden
                else "within token guardrail"
            ),
            estimated_input_tokens=estimated_input,
            estimated_output_tokens=expected_output_tokens,
            cache_key=cache_key,
            truncated_system=truncated_system,
            truncated_user=truncated_user,
            budget_overridden=budget_overridden,
            verified_required=verified_required,
        )

    def get_cached(self, cache_key: str) -> Optional[str]:
        if not self.config.enable_cache:
            return None
        cutoff = time.time() - self.config.cache_ttl_seconds
        with connect_sqlite(self.db_path) as con:
            row = con.execute(
                "SELECT response, created_at FROM llm_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if not row:
            return None
        response, created_at = row
        if created_at < cutoff:
            return None
        return str(response)

    def put_cached(self, *, cache_key: str, model: str, response: str) -> None:
        if not self.config.enable_cache:
            return
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                INSERT OR REPLACE INTO llm_cache
                (cache_key, model, response, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (cache_key, model, response, time.time()),
            )

    def record_call(
        self,
        *,
        task_id: str,
        model: str,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
        verified_required: bool,
        budget_overridden: bool,
        reason: str,
    ) -> None:
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                INSERT INTO llm_usage
                (
                    task_id,
                    model,
                    estimated_input_tokens,
                    estimated_output_tokens,
                    verified_required,
                    budget_overridden,
                    reason,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    model,
                    estimated_input_tokens,
                    estimated_output_tokens,
                    1 if verified_required else 0,
                    1 if budget_overridden else 0,
                    reason,
                    time.time(),
                ),
            )
