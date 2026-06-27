from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, field_validator


TRUE_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "off"}


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _optional_env(name: str) -> Optional[str]:
    value = _env(name)
    return value or None


def _bool_env(name: str, default: bool) -> bool:
    value = _env(name).lower()
    if not value:
        return default
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    return default


def _int_env(name: str, default: int) -> int:
    value = _env(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _datetime_env(name: str) -> Optional[datetime]:
    value = _env(name)
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except ValueError:
        pass
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class BigSellerConfig(BaseModel):
    """Runtime configuration for the BigSeller connector.

    BigSeller API access is private-approval based. This config deliberately
    stores credentials and transport settings without declaring unverified API
    endpoints, signature algorithms, or field names.
    """

    enabled: bool = False
    base_url: Optional[str] = None
    app_id: Optional[str] = None
    app_key: Optional[str] = None
    auth_code: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_expires_at: Optional[datetime] = None
    webhook_secret: Optional[str] = None
    sync_interval_seconds: int = 300
    max_retries: int = 3
    rate_limit_per_minute: int = 60
    use_mock: bool = True
    audit_log_path: Path = Field(
        default_factory=lambda: Path("~/.omnidesk/bigseller_audit.jsonl").expanduser()
    )
    state_db_path: Path = Field(
        default_factory=lambda: Path("~/.omnidesk/bigseller_state.sqlite3").expanduser()
    )

    @field_validator("base_url")
    @classmethod
    def _strip_base_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip().rstrip("/")
        return stripped or None

    @classmethod
    def from_env(cls, *, workspace_root: Path | None = None) -> "BigSellerConfig":
        audit_log = Path(
            os.getenv("BIGSELLER_AUDIT_LOG_PATH", "")
            or "~/.omnidesk/bigseller_audit.jsonl"
        ).expanduser()
        state_db = Path(
            os.getenv("BIGSELLER_STATE_DB_PATH", "")
            or "~/.omnidesk/bigseller_state.sqlite3"
        ).expanduser()
        if workspace_root is not None:
            audit_log = Path(
                os.getenv("BIGSELLER_AUDIT_LOG_PATH", "")
                or str(workspace_root / "bigseller_audit.jsonl")
            ).expanduser()
            state_db = Path(
                os.getenv("BIGSELLER_STATE_DB_PATH", "")
                or str(workspace_root / "bigseller_state.sqlite3")
            ).expanduser()
        return cls(
            enabled=_bool_env("BIGSELLER_ENABLED", False),
            base_url=_optional_env("BIGSELLER_BASE_URL"),
            app_id=_optional_env("BIGSELLER_APP_ID"),
            app_key=_optional_env("BIGSELLER_APP_KEY"),
            auth_code=_optional_env("BIGSELLER_AUTH_CODE"),
            access_token=_optional_env("BIGSELLER_ACCESS_TOKEN"),
            refresh_token=_optional_env("BIGSELLER_REFRESH_TOKEN"),
            token_expires_at=_datetime_env("BIGSELLER_TOKEN_EXPIRES_AT"),
            webhook_secret=_optional_env("BIGSELLER_WEBHOOK_SECRET"),
            sync_interval_seconds=max(
                1, _int_env("BIGSELLER_SYNC_INTERVAL_SECONDS", 300)
            ),
            max_retries=max(0, _int_env("BIGSELLER_MAX_RETRIES", 3)),
            rate_limit_per_minute=max(
                1, _int_env("BIGSELLER_RATE_LIMIT_PER_MINUTE", 60)
            ),
            use_mock=_bool_env("BIGSELLER_USE_MOCK", True),
            audit_log_path=audit_log,
            state_db_path=state_db,
        )

    @property
    def mode(self) -> str:
        return "mock" if self.use_mock else "real"

    def real_mode_issues(self) -> list[str]:
        if self.use_mock or not self.enabled:
            return []
        issues: list[str] = []
        for name, value in (
            ("BIGSELLER_BASE_URL", self.base_url),
            ("BIGSELLER_APP_ID", self.app_id),
            ("BIGSELLER_APP_KEY", self.app_key),
        ):
            if not value:
                issues.append(f"{name} is required for BigSeller real mode")
        if not (self.access_token or self.auth_code):
            issues.append(
                "BIGSELLER_ACCESS_TOKEN or BIGSELLER_AUTH_CODE is required for BigSeller real mode"
            )
        return issues

    def health(self) -> dict[str, object]:
        issues = self.real_mode_issues()
        ready = self.enabled and (self.use_mock or not issues)
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "ready": ready,
            "issues": issues,
        }

    def redacted(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "base_url": self.base_url,
            "app_id_configured": bool(self.app_id),
            "app_key_configured": bool(self.app_key),
            "auth_code_configured": bool(self.auth_code),
            "access_token_configured": bool(self.access_token),
            "refresh_token_configured": bool(self.refresh_token),
            "token_expires_at": self.token_expires_at.isoformat()
            if self.token_expires_at
            else None,
            "webhook_secret_configured": bool(self.webhook_secret),
            "sync_interval_seconds": self.sync_interval_seconds,
            "max_retries": self.max_retries,
            "rate_limit_per_minute": self.rate_limit_per_minute,
        }
