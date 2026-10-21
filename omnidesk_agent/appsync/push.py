from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

# Push targets are stored as plain FCM registration tokens (legacy clients) or
# as "fid:<Firebase Installation ID>" (firebase_messaging >= 17 register() /
# onRegistered). FCM registration tokens always have ":" after a 22-character
# instance id, so they can never start with "fid:".
FID_PREFIX = "fid:"
PUSH_TOKEN_TYPES = frozenset({"fcm_token", "fid"})
_FID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")


def normalize_push_target(value: object, token_type: object = "fcm_token") -> str | None:
    """Validates a client-supplied push target and returns its stored form."""
    target = str(value or "").strip()
    if not target:
        return None
    kind = str(token_type or "fcm_token")
    if kind not in PUSH_TOKEN_TYPES:
        raise ValueError("token_type must be fcm_token or fid")
    if kind == "fid":
        if not _FID_RE.fullmatch(target):
            raise ValueError("invalid Firebase Installation ID")
        return FID_PREFIX + target
    if target.startswith(FID_PREFIX) or len(target) > 4096:
        raise ValueError("invalid FCM registration token")
    return target


def split_push_target(stored: str) -> tuple[str, str]:
    """Returns (kind, value) for a stored push target."""
    if stored.startswith(FID_PREFIX):
        return "fid", stored[len(FID_PREFIX):]
    return "fcm_token", stored

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
            stored = str(item.get("push_token") or item.get("token") or "")
            if not stored:
                raise ValueError("push token is required")
            kind, target = split_push_target(stored)
            content = {
                "notification": messaging.Notification(title=str(item.get("title") or "Omni"), body=str(item.get("body") or "")),
                "data": {"push_id": str(item.get("push_id") or ""), "related_id": str(item.get("related_id") or ""), "audience": str(item.get("audience") or "")},
            }
            message_id = messaging.send(_build_message(messaging, kind, target, content), app=self.app)
            return PushDeliveryResult(push_id=str(item.get("push_id")), ok=True, provider=self.name, provider_message_id=message_id)
        except Exception as exc:
            return PushDeliveryResult(push_id=str(item.get("push_id")), ok=False, provider=self.name, error=str(exc))

def _build_message(messaging: Any, kind: str, target: str, content: dict[str, Any]) -> Any:
    if kind == "fid":
        try:
            return messaging.Message(fid=target, **content)
        except TypeError:
            # firebase_admin releases without the `fid` field: FCM v1 accepts an
            # FID in `token` during the FID transition period.
            return messaging.Message(token=target, **content)
    return messaging.Message(token=target, **content)


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
