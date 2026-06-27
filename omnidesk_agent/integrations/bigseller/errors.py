from __future__ import annotations

from datetime import timedelta
import hashlib
import re
import uuid
from threading import Lock
from typing import Any, Optional

from omnidesk_agent.integrations.bigseller.schemas import BigSellerQueuedError, utc_now


SECRET_KEY_RE = re.compile(
    r"(token|secret|password|app[_-]?key|authorization|cookie)", re.IGNORECASE
)
SECRET_VALUE_RE = re.compile(
    r"(?i)(access[_-]?token|refresh[_-]?token|app[_-]?key|secret|authorization)\s*[:=]\s*[^,}]+"
)


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]"
            if SECRET_KEY_RE.search(str(key))
            else redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return SECRET_VALUE_RE.sub(
            lambda match: (
                match.group(0).split(match.group(1), 1)[0]
                + match.group(1)
                + "=[REDACTED]"
            ),
            value,
        )
    return value


class BigSellerError(Exception):
    code = "bigseller_error"

    def __init__(self, message: str, *, code: Optional[str] = None):
        self.safe_message = str(redact_secrets(message))
        if code:
            self.code = code
        super().__init__(self.safe_message)


class BigSellerDisabledError(BigSellerError):
    code = "bigseller_disabled"


class BigSellerConfigurationError(BigSellerError):
    code = "bigseller_configuration_error"


class BigSellerAuthenticationError(BigSellerError):
    code = "bigseller_authentication_error"


class BigSellerUnauthorizedError(BigSellerAuthenticationError):
    code = "bigseller_unauthorized"


class BigSellerRateLimitError(BigSellerError):
    code = "bigseller_rate_limited"


class BigSellerEndpointNotConfigured(BigSellerConfigurationError):
    code = "bigseller_endpoint_not_configured"


class BigSellerRetryableError(BigSellerError):
    code = "bigseller_retryable_error"


class BigSellerSyncErrorQueue:
    """Process-local retry/dead-letter queue for connector record failures."""

    def __init__(self, *, max_retries: int = 3):
        self.max_retries = max(0, int(max_retries))
        self._items: dict[str, BigSellerQueuedError] = {}
        self._lock = Lock()

    @staticmethod
    def _key(entity_type: str, external_id: str, store_id: str, action: str) -> str:
        digest = hashlib.sha256(
            f"{entity_type}:{store_id}:{external_id}:{action}".encode("utf-8")
        ).hexdigest()
        return digest[:32]

    def enqueue(
        self,
        *,
        entity_type: str,
        external_id: str,
        store_id: str,
        action: str,
        payload: dict[str, Any],
        error: BaseException | str,
        error_code: str = "sync_error",
    ) -> BigSellerQueuedError:
        now = utc_now()
        error_message = str(redact_secrets(str(error)))[:1000]
        item_id = self._key(entity_type, external_id, store_id, action)
        with self._lock:
            existing = self._items.get(item_id)
            retry_count = 1 if existing is None else existing.retry_count + 1
            status = "dead_letter" if retry_count > self.max_retries else "retryable"
            next_retry_at = (
                now + timedelta(seconds=30 * (2 ** max(0, retry_count - 1)))
                if status == "retryable"
                else now
            )
            item = BigSellerQueuedError(
                id=existing.id if existing else str(uuid.uuid4()),
                entity_type=entity_type,
                external_id=external_id,
                store_id=store_id,
                action=action,
                payload=redact_secrets(payload),
                status=status,
                retry_count=retry_count,
                max_retries=self.max_retries,
                error_code=error_code,
                error_message=error_message,
                next_retry_at=next_retry_at,
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
            self._items[item_id] = item
            return item

    def resolve(
        self, *, entity_type: str, external_id: str, store_id: str, action: str
    ) -> None:
        with self._lock:
            item = self._items.get(
                self._key(entity_type, external_id, store_id, action)
            )
            if item is not None:
                item.status = "resolved"
                item.updated_at = utc_now()

    def list(
        self, *, status: str | None = None, limit: int = 50
    ) -> list[BigSellerQueuedError]:
        with self._lock:
            items = sorted(
                self._items.values(), key=lambda item: item.updated_at, reverse=True
            )
        if status:
            items = [item for item in items if item.status == status]
        return items[: max(0, limit)]

    def stats(self) -> dict[str, int]:
        with self._lock:
            items = list(self._items.values())
        return {
            "retryable": sum(1 for item in items if item.status == "retryable"),
            "dead_letter": sum(1 for item in items if item.status == "dead_letter"),
            "resolved": sum(1 for item in items if item.status == "resolved"),
            "total": len(items),
        }
