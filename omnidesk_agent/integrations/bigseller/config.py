from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, field_validator


TRUE_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "off"}
STATE_BACKENDS = {"memory", "sqlite", "postgres"}


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


def _json_dict_env(name: str) -> dict[str, str]:
    value = _env(name)
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(item) for key, item in parsed.items() if item is not None}


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

    BigSeller API access is private-approval based. Real mode is intentionally
    fail-closed until official endpoint paths, signing behavior, credentials,
    durable state, and webhook verification are configured.
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
    state_backend: str = "sqlite"
    postgres_dsn: Optional[str] = None
    webhook_replay_window_seconds: int = 300
    webhook_event_ttl_seconds: int = 86400
    webhook_max_body_bytes: int = 262144

    auth_code_exchange_path: Optional[str] = None
    refresh_token_path: Optional[str] = None
    orders_list_path: Optional[str] = None
    order_detail_path: Optional[str] = None
    inventory_list_path: Optional[str] = None
    inventory_update_path: Optional[str] = None
    products_list_path: Optional[str] = None
    product_detail_path: Optional[str] = None
    fulfillment_sync_path: Optional[str] = None
    request_signing_enabled: bool = False
    signature_header: str = "x-bigseller-signature"
    signature_timestamp_header: str = "x-bigseller-timestamp"
    signature_app_id_header: str = "x-bigseller-app-id"
    response_root_keys: dict[str, str] = Field(default_factory=dict)

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

    @field_validator(
        "auth_code_exchange_path",
        "refresh_token_path",
        "orders_list_path",
        "order_detail_path",
        "inventory_list_path",
        "inventory_update_path",
        "products_list_path",
        "product_detail_path",
        "fulfillment_sync_path",
    )
    @classmethod
    def _normalize_endpoint_path(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        return normalized if normalized.startswith("/") else f"/{normalized}"

    @field_validator("state_backend")
    @classmethod
    def _normalize_state_backend(cls, value: str) -> str:
        normalized = (value or "sqlite").strip().lower()
        if normalized not in STATE_BACKENDS:
            raise ValueError(
                "BIGSELLER_STATE_BACKEND must be one of memory, sqlite, postgres"
            )
        return normalized

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
            state_backend=(_env("BIGSELLER_STATE_BACKEND") or "sqlite"),
            postgres_dsn=_optional_env("BIGSELLER_POSTGRES_DSN"),
            webhook_replay_window_seconds=max(
                1, _int_env("BIGSELLER_WEBHOOK_REPLAY_WINDOW_SECONDS", 300)
            ),
            webhook_event_ttl_seconds=max(
                60, _int_env("BIGSELLER_WEBHOOK_EVENT_TTL_SECONDS", 86400)
            ),
            webhook_max_body_bytes=max(
                1024, _int_env("BIGSELLER_WEBHOOK_MAX_BODY_BYTES", 262144)
            ),
            auth_code_exchange_path=_optional_env("BIGSELLER_AUTH_CODE_EXCHANGE_PATH"),
            refresh_token_path=_optional_env("BIGSELLER_REFRESH_TOKEN_PATH"),
            orders_list_path=_optional_env("BIGSELLER_ORDERS_LIST_PATH"),
            order_detail_path=_optional_env("BIGSELLER_ORDER_DETAIL_PATH"),
            inventory_list_path=_optional_env("BIGSELLER_INVENTORY_LIST_PATH"),
            inventory_update_path=_optional_env("BIGSELLER_INVENTORY_UPDATE_PATH"),
            products_list_path=_optional_env("BIGSELLER_PRODUCTS_LIST_PATH"),
            product_detail_path=_optional_env("BIGSELLER_PRODUCT_DETAIL_PATH"),
            fulfillment_sync_path=_optional_env("BIGSELLER_FULFILLMENT_SYNC_PATH"),
            request_signing_enabled=_bool_env("BIGSELLER_REQUEST_SIGNING_ENABLED", False),
            signature_header=_env("BIGSELLER_SIGNATURE_HEADER") or "x-bigseller-signature",
            signature_timestamp_header=_env("BIGSELLER_SIGNATURE_TIMESTAMP_HEADER") or "x-bigseller-timestamp",
            signature_app_id_header=_env("BIGSELLER_SIGNATURE_APP_ID_HEADER") or "x-bigseller-app-id",
            response_root_keys=_json_dict_env("BIGSELLER_RESPONSE_ROOT_KEYS"),
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
            ("BIGSELLER_WEBHOOK_SECRET", self.webhook_secret),
        ):
            if not value:
                issues.append(f"{name} is required for BigSeller real mode")
        if not (self.access_token or self.auth_code):
            issues.append(
                "BIGSELLER_ACCESS_TOKEN or BIGSELLER_AUTH_CODE is required for BigSeller real mode"
            )
        for name, value in (
            ("BIGSELLER_ORDERS_LIST_PATH", self.orders_list_path),
            ("BIGSELLER_ORDER_DETAIL_PATH", self.order_detail_path),
            ("BIGSELLER_INVENTORY_LIST_PATH", self.inventory_list_path),
            ("BIGSELLER_INVENTORY_UPDATE_PATH", self.inventory_update_path),
            ("BIGSELLER_PRODUCTS_LIST_PATH", self.products_list_path),
            ("BIGSELLER_PRODUCT_DETAIL_PATH", self.product_detail_path),
            ("BIGSELLER_FULFILLMENT_SYNC_PATH", self.fulfillment_sync_path),
        ):
            if not value:
                issues.append(f"{name} is required for BigSeller real mode")
        if self.auth_code and not self.auth_code_exchange_path:
            issues.append(
                "BIGSELLER_AUTH_CODE_EXCHANGE_PATH is required when BIGSELLER_AUTH_CODE is configured"
            )
        if self.refresh_token and not self.refresh_token_path:
            issues.append(
                "BIGSELLER_REFRESH_TOKEN_PATH is required when BIGSELLER_REFRESH_TOKEN is configured"
            )
        if self.request_signing_enabled and not self.app_key:
            issues.append(
                "BIGSELLER_APP_KEY is required when BIGSELLER_REQUEST_SIGNING_ENABLED=true"
            )
        if self.state_backend == "memory":
            issues.append(
                "BIGSELLER_STATE_BACKEND=memory is not allowed for BigSeller real mode"
            )
        if self.state_backend == "postgres" and not self.postgres_dsn:
            issues.append(
                "BIGSELLER_POSTGRES_DSN is required when BIGSELLER_STATE_BACKEND=postgres"
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
            "state_backend": self.state_backend,
            "durable_state": self.state_backend in {"sqlite", "postgres"},
            "real_endpoint_contract_configured": self.use_mock or not issues,
            "request_signing_enabled": self.request_signing_enabled,
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
            "state_backend": self.state_backend,
            "postgres_configured": bool(self.postgres_dsn),
            "state_db_path": str(self.state_db_path),
            "webhook_replay_window_seconds": self.webhook_replay_window_seconds,
            "webhook_event_ttl_seconds": self.webhook_event_ttl_seconds,
            "webhook_max_body_bytes": self.webhook_max_body_bytes,
            "endpoint_paths_configured": {
                "auth_code_exchange": bool(self.auth_code_exchange_path),
                "refresh_token": bool(self.refresh_token_path),
                "orders_list": bool(self.orders_list_path),
                "order_detail": bool(self.order_detail_path),
                "inventory_list": bool(self.inventory_list_path),
                "inventory_update": bool(self.inventory_update_path),
                "products_list": bool(self.products_list_path),
                "product_detail": bool(self.product_detail_path),
                "fulfillment_sync": bool(self.fulfillment_sync_path),
            },
            "request_signing_enabled": self.request_signing_enabled,
            "signature_header": self.signature_header,
            "signature_timestamp_header": self.signature_timestamp_header,
            "signature_app_id_header": self.signature_app_id_header,
            "response_root_keys": dict(self.response_root_keys),
        }
