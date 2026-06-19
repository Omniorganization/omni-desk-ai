from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

@dataclass
class PushDeliveryResult:
    push_id: str
    ok: bool
    provider: str
    error: str | None = None
    provider_message_id: str | None = None

class PushProvider(Protocol):
    name: str
    def send(self, item: dict[str, Any]) -> PushDeliveryResult: ...

class DryRunPushProvider:
    name = "dry_run"
    def send(self, item: dict[str, Any]) -> PushDeliveryResult:
        return PushDeliveryResult(push_id=str(item.get("push_id")), ok=True, provider=self.name, provider_message_id="dry-run")

class FirebasePushProvider:
    """Firebase Admin SDK push adapter.

    Requires firebase_admin and either FIREBASE_SERVICE_ACCOUNT_JSON or
    GOOGLE_APPLICATION_CREDENTIALS in release environments. The adapter is lazy
    so source checks do not need Firebase credentials.
    """
    name = "firebase"
    def __init__(self, app: Any | None = None):
        self.app = app
    def _messaging(self) -> Any:
        try:
            import firebase_admin
            from firebase_admin import credentials, messaging
        except Exception as exc:  # pragma: no cover - release env only
            raise RuntimeError("firebase_admin is required for FirebasePushProvider") from exc
        if self.app is None:
            if not firebase_admin._apps:
                raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
                if raw:
                    cred = credentials.Certificate(json.loads(raw))
                    self.app = firebase_admin.initialize_app(cred)
                else:
                    self.app = firebase_admin.initialize_app()
            else:
                self.app = firebase_admin.get_app()
        return messaging
    def send(self, item: dict[str, Any]) -> PushDeliveryResult:
        try:
            messaging = self._messaging()
            token = str(item.get("push_token") or item.get("token") or "")
            if not token:
                raise ValueError("push token is required")
            message = messaging.Message(
                token=token,
                notification=messaging.Notification(title=str(item.get("title") or "Omni"), body=str(item.get("body") or "")),
                data={"push_id": str(item.get("push_id") or ""), "related_id": str(item.get("related_id") or ""), "audience": str(item.get("audience") or "")},
            )
            message_id = messaging.send(message, app=self.app)
            return PushDeliveryResult(push_id=str(item.get("push_id")), ok=True, provider=self.name, provider_message_id=message_id)
        except Exception as exc:
            return PushDeliveryResult(push_id=str(item.get("push_id")), ok=False, provider=self.name, error=str(exc))

class ApnsPushProvider:
    """APNS release adapter seam.

    A production deployment injects a client exposing send(item). This keeps the
    OSS package source-testable while making APNS a first-class release gate.
    """
    name = "apns"
    def __init__(self, client: Any):
        self.client = client
    def send(self, item: dict[str, Any]) -> PushDeliveryResult:
        try:
            message_id = self.client.send(item)
            return PushDeliveryResult(push_id=str(item.get("push_id")), ok=True, provider=self.name, provider_message_id=str(message_id))
        except Exception as exc:
            return PushDeliveryResult(push_id=str(item.get("push_id")), ok=False, provider=self.name, error=str(exc))

def provider_from_env() -> PushProvider:
    provider = os.environ.get("OMNIDESK_PUSH_PROVIDER", "dry_run").strip().lower()
    if provider == "firebase":
        return FirebasePushProvider()
    return DryRunPushProvider()

def dispatch_pending_push(store: Any, provider: PushProvider | None = None, limit: int = 100) -> list[PushDeliveryResult]:
    provider = provider or provider_from_env()
    results: list[PushDeliveryResult] = []
    for item in store.pending_push_outbox(limit=limit):
        result = provider.send(item)
        store.mark_push_delivery(push_id=result.push_id, status="sent" if result.ok else "failed", error=result.error)
        results.append(result)
    return results
