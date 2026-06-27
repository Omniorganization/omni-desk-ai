from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from omnidesk_agent.integrations.bigseller.schemas import utc_now


@dataclass(frozen=True)
class BigSellerIdempotencyKey:
    external_id: str
    store_id: str
    action_type: str

    @property
    def value(self) -> str:
        return f"{self.store_id}:{self.external_id}:{self.action_type}"


class BigSellerIdempotencyGuard:
    """Guards repeated connector side effects within the running process."""

    def __init__(self):
        self._records: dict[str, dict[str, object]] = {}
        self._lock = Lock()

    def claim(self, *, external_id: str, store_id: str, action_type: str) -> bool:
        key = BigSellerIdempotencyKey(
            external_id=external_id, store_id=store_id, action_type=action_type
        ).value
        now = utc_now().isoformat()
        with self._lock:
            existing = self._records.get(key)
            if existing is not None and existing.get("status") in {
                "in_progress",
                "completed",
            }:
                return False
            self._records[key] = {
                "external_id": external_id,
                "store_id": store_id,
                "action_type": action_type,
                "status": "in_progress",
                "created_at": existing.get("created_at") if existing else now,
                "updated_at": now,
            }
            return True

    def complete(self, *, external_id: str, store_id: str, action_type: str) -> None:
        self._set_status(
            external_id=external_id,
            store_id=store_id,
            action_type=action_type,
            status="completed",
        )

    def release(self, *, external_id: str, store_id: str, action_type: str) -> None:
        key = BigSellerIdempotencyKey(
            external_id=external_id, store_id=store_id, action_type=action_type
        ).value
        with self._lock:
            self._records.pop(key, None)

    def _set_status(
        self, *, external_id: str, store_id: str, action_type: str, status: str
    ) -> None:
        key = BigSellerIdempotencyKey(
            external_id=external_id, store_id=store_id, action_type=action_type
        ).value
        with self._lock:
            record = self._records.setdefault(
                key,
                {
                    "external_id": external_id,
                    "store_id": store_id,
                    "action_type": action_type,
                    "created_at": utc_now().isoformat(),
                },
            )
            record["status"] = status
            record["updated_at"] = utc_now().isoformat()

    def stats(self) -> dict[str, int]:
        with self._lock:
            values = list(self._records.values())
        return {
            "total": len(values),
            "completed": sum(1 for item in values if item.get("status") == "completed"),
            "in_progress": sum(
                1 for item in values if item.get("status") == "in_progress"
            ),
        }
