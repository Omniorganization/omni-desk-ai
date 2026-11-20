from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger(__name__)

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
    # A failure worth retrying later (quota, provider outage, network).
    retryable: bool = False
    # The provider no longer knows the target (FCM UNREGISTERED / sender mismatch).
    invalid_target: bool = False


class PushSendError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, invalid_target: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.invalid_target = invalid_target


# FCM HTTP v1 error statuses (https://firebase.google.com/docs/reference/fcm/rest/v1/ErrorCode).
_FCM_INVALID_TARGET = frozenset({"UNREGISTERED", "SENDER_ID_MISMATCH"})
_FCM_RETRYABLE = frozenset({"QUOTA_EXCEEDED", "UNAVAILABLE", "INTERNAL", "THIRD_PARTY_AUTH_ERROR"})


def classify_fcm_error(http_status: int, status: str) -> PushSendError:
    message = f"FCM send failed: HTTP {http_status} {status}".strip()
    if status in _FCM_INVALID_TARGET or http_status == 404:
        return PushSendError(message, retryable=False, invalid_target=True)
    if status in _FCM_RETRYABLE or http_status == 429 or http_status >= 500:
        return PushSendError(message, retryable=True)
    return PushSendError(message, retryable=False)

class PushProvider(Protocol):
    name: str
    def send(self, item: dict[str, Any]) -> PushDeliveryResult: ...

class DryRunPushProvider:
    name = "dry_run"
    def send(self, item: dict[str, Any]) -> PushDeliveryResult:
        return PushDeliveryResult(push_id=str(item.get("push_id")), ok=True, provider=self.name, provider_message_id="dry-run")

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def load_service_account() -> dict[str, Any]:
    """Reads FIREBASE_SERVICE_ACCOUNT_JSON (JSON text) or GOOGLE_APPLICATION_CREDENTIALS (file path)."""
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if not path:
            raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS is required for FCM")
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    account = json.loads(raw)
    for field_name in ("client_email", "private_key", "project_id"):
        if not account.get(field_name):
            raise RuntimeError(f"service account JSON is missing {field_name}")
    if not _PROJECT_ID_RE.fullmatch(str(account["project_id"])):
        raise RuntimeError("service account project_id is invalid")
    # Never send the signed assertion anywhere but Google's token endpoint.
    if account.get("token_uri", GOOGLE_TOKEN_URI) != GOOGLE_TOKEN_URI:
        raise RuntimeError("service account token_uri must be https://oauth2.googleapis.com/token")
    return account


class FcmHttpV1Client:
    """Minimal FCM HTTP v1 sender (service-account OAuth, no SDK dependency).

    Uses the FCM v1 ``fid`` field for Firebase Installation IDs and ``token``
    for legacy registration tokens.
    """

    def __init__(self, account: dict[str, Any], http: Any | None = None, timeout: float = 15.0):
        import httpx
        from cryptography.hazmat.primitives import serialization

        self._email = str(account["client_email"])
        self._project_id = str(account["project_id"])
        self._key = serialization.load_pem_private_key(str(account["private_key"]).encode("utf-8"), password=None)
        self._http = http or httpx.Client(timeout=timeout)
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()

    def _assertion(self, now: int) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
        claims = _b64url(json.dumps({
            "iss": self._email, "scope": FCM_SCOPE, "aud": GOOGLE_TOKEN_URI, "iat": now, "exp": now + 3600,
        }, separators=(",", ":")).encode())
        signing_input = f"{header}.{claims}".encode("ascii")
        signature = self._key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]
        return f"{header}.{claims}.{_b64url(signature)}"

    def _access_token(self) -> str:
        with self._lock:
            now = time.time()
            if self._token and self._token[1] - 60 > now:
                return self._token[0]
            response = self._http.post(GOOGLE_TOKEN_URI, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": self._assertion(int(now)),
            })
            if response.status_code != 200:
                raise PushSendError(f"FCM OAuth token request failed: HTTP {response.status_code}", retryable=True)
            payload = response.json()
            self._token = (str(payload["access_token"]), now + float(payload.get("expires_in", 3600)))
            return self._token[0]

    def send(self, kind: str, target: str, notification: dict[str, str], data: dict[str, str]) -> str:
        message: dict[str, Any] = {"notification": notification, "data": data}
        message["fid" if kind == "fid" else "token"] = target
        response = self._http.post(
            f"https://fcm.googleapis.com/v1/projects/{self._project_id}/messages:send",
            headers={"authorization": f"Bearer {self._access_token()}"},
            json={"message": message},
        )
        if response.status_code != 200:
            try:
                status = str(response.json().get("error", {}).get("status", ""))
            except ValueError:
                status = ""
            raise classify_fcm_error(response.status_code, status)
        return str(response.json().get("name", ""))


class FirebasePushProvider:
    """Firebase Cloud Messaging push adapter.

    Uses the Firebase Admin SDK when it is installed (firebase_admin >= 7.5.0
    for Firebase Installation IDs), otherwise the built-in FCM HTTP v1 client,
    which needs only FIREBASE_SERVICE_ACCOUNT_JSON or
    GOOGLE_APPLICATION_CREDENTIALS. The adapter is lazy so source checks do not
    need Firebase credentials.
    """
    name = "firebase"
    def __init__(self, app: Any | None = None, http_client: FcmHttpV1Client | None = None):
        self.app = app
        self._http_client = http_client

    def _sdk_available(self) -> bool:
        if self._http_client is not None:
            return False
        try:
            import firebase_admin  # noqa: F401
        except Exception:
            return False
        return True

    def _http_v1(self) -> FcmHttpV1Client:
        if self._http_client is None:
            self._http_client = FcmHttpV1Client(load_service_account())
        return self._http_client

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
            stored = str(item.get("push_token") or item.get("token") or "")
            if not stored:
                raise ValueError("push token is required")
            kind, target = split_push_target(stored)
            data = {
                "push_id": str(item.get("push_id") or ""),
                "related_id": str(item.get("related_id") or ""),
                "audience": str(item.get("audience") or ""),
                "event_type": str(item.get("event_type") or ""),
            }
            if not self._sdk_available():
                name = self._http_v1().send(kind, target, {"title": str(item.get("title") or "Omni"), "body": str(item.get("body") or "")}, data)
                return PushDeliveryResult(push_id=str(item.get("push_id")), ok=True, provider=self.name, provider_message_id=name)
            messaging = self._messaging()
            content = {
                "notification": messaging.Notification(title=str(item.get("title") or "Omni"), body=str(item.get("body") or "")),
                "data": data,
            }
            message_id = messaging.send(_build_message(messaging, kind, target, content), app=self.app)
            return PushDeliveryResult(push_id=str(item.get("push_id")), ok=True, provider=self.name, provider_message_id=message_id)
        except PushSendError as exc:
            return PushDeliveryResult(push_id=str(item.get("push_id")), ok=False, provider=self.name, error=str(exc), retryable=exc.retryable, invalid_target=exc.invalid_target)
        except ValueError as exc:
            return PushDeliveryResult(push_id=str(item.get("push_id")), ok=False, provider=self.name, error=str(exc))
        except Exception as exc:
            # firebase_admin.messaging raises typed errors; anything unknown
            # (network, provider outage) is retried until the attempts run out.
            name = type(exc).__name__
            return PushDeliveryResult(
                push_id=str(item.get("push_id")), ok=False, provider=self.name, error=f"{name}: {exc}",
                retryable=name not in _SDK_PERMANENT_ERRORS and name not in _SDK_INVALID_TARGET_ERRORS,
                invalid_target=name in _SDK_INVALID_TARGET_ERRORS,
            )


_SDK_INVALID_TARGET_ERRORS = frozenset({"UnregisteredError", "SenderIdMismatchError"})
_SDK_PERMANENT_ERRORS = frozenset({"InvalidArgumentError"})

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

PUSH_SEND_TIMEOUT_SECONDS = 15.0


def _env_number(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc


@dataclass(frozen=True)
class PushRetryPolicy:
    """Retries a retryable failure after 10 s, 20 s, 40 s ... (at most 15 min apart).

    A push is given up after ``max_attempts`` sends, or when it is older than
    ``max_age_seconds``: an approval request from yesterday is not worth a ping.
    """

    max_attempts: int = 5
    base_delay_seconds: float = 10.0
    max_delay_seconds: float = 900.0
    max_age_seconds: float = 86_400.0

    def retry_at(self, attempt: int, now: float) -> float:
        return now + min(self.max_delay_seconds, self.base_delay_seconds * (2 ** max(0, attempt - 1)))


def push_policy_from_env() -> PushRetryPolicy:
    return PushRetryPolicy(
        max_attempts=max(1, int(_env_number("OMNIDESK_PUSH_MAX_ATTEMPTS", 5))),
        max_age_seconds=max(60.0, _env_number("OMNIDESK_PUSH_MAX_AGE_SECONDS", 86_400.0)),
    )


def new_dispatcher_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"


def dispatch_pending_push(
    store: Any,
    provider: PushProvider | None = None,
    limit: int = 100,
    *,
    worker_id: Optional[str] = None,
    policy: Optional[PushRetryPolicy] = None,
    clock: Callable[[], float] = time.time,
) -> list[PushDeliveryResult]:
    """Leases due pushes, sends each once, and records the outcome.

    Delivery is at least once: if this process dies between the provider's
    acceptance and the record, the lease expires and the push is sent again.
    """
    provider = provider or provider_from_env()
    policy = policy or push_policy_from_env()
    worker = worker_id or new_dispatcher_id()
    limit = max(1, min(int(limit), 500))
    # The lease covers the whole batch, so no other dispatcher takes an item
    # this one has not reached yet.
    lease = 30.0 + limit * PUSH_SEND_TIMEOUT_SECONDS
    results: list[PushDeliveryResult] = []
    claimed = store.claim_push_deliveries(worker_id=worker, limit=limit, lease_seconds=lease, max_attempts=policy.max_attempts, max_age_seconds=policy.max_age_seconds)
    for item in claimed:
        result = provider.send(item)
        attempt = int(item.get("attempt_count") or 1)
        target = str(item.get("push_token") or "") or None
        if result.ok:
            outcome = "dry_run" if result.provider == DryRunPushProvider.name else "sent"
        elif result.retryable and attempt < policy.max_attempts:
            outcome = "retry"
        else:
            outcome = "failed"
        store.complete_push_delivery(
            push_id=result.push_id, worker_id=worker, outcome=outcome, provider=result.provider,
            error=result.error, provider_message_id=result.provider_message_id,
            retry_at=policy.retry_at(attempt, clock()) if outcome == "retry" else None,
            invalid_target=target if result.invalid_target else None,
        )
        results.append(result)
    return results


class PushDispatcher:
    """Sends due pushes every ``interval_seconds`` on a daemon thread.

    Every replica runs one; leases keep them from sending the same push. A full
    batch is followed by the next one at once, so a backlog drains quickly.
    """

    def __init__(self, store: Any, provider: PushProvider, *, interval_seconds: float = 5.0, batch_size: int = 20, policy: Optional[PushRetryPolicy] = None, metrics: Any = None) -> None:
        self.store = store
        self.provider = provider
        self.interval_seconds = max(0.05, float(interval_seconds))
        self.batch_size = max(1, int(batch_size))
        self.policy = policy or push_policy_from_env()
        self.metrics = metrics
        self.worker_id = new_dispatcher_id()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="omnidesk-push-dispatch", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def run_once(self) -> list[PushDeliveryResult]:
        results = dispatch_pending_push(self.store, self.provider, self.batch_size, worker_id=self.worker_id, policy=self.policy)
        if self.metrics is not None:
            for result in results:
                self.metrics.inc("omnidesk_push_deliveries_total", provider=result.provider, outcome="sent" if result.ok else ("retryable" if result.retryable else "failed"))
        return results

    def _run(self) -> None:
        delay = self.interval_seconds
        while not self._stop.wait(delay):
            try:
                full = len(self.run_once()) >= self.batch_size
            except Exception:
                logger.exception("push dispatch failed")
                if self.metrics is not None:
                    self.metrics.inc("omnidesk_push_dispatch_failures_total")
                full = False
            delay = 0.0 if full else self.interval_seconds


def start_push_dispatcher(store: Any, metrics: Any = None, provider: Optional[PushProvider] = None) -> Optional[PushDispatcher]:
    """Starts automatic dispatch when a real provider is configured.

    With the default dry-run provider nothing would reach a device, so nothing
    is dispatched automatically; ``OMNIDESK_PUSH_DISPATCH_INTERVAL_SECONDS=0``
    turns it off (an owner can still POST /app/push/dispatch).
    """
    provider = provider or provider_from_env()
    if provider.name == DryRunPushProvider.name or not callable(getattr(store, "claim_push_deliveries", None)):
        return None
    interval = _env_number("OMNIDESK_PUSH_DISPATCH_INTERVAL_SECONDS", 5.0)
    if interval <= 0:
        return None
    dispatcher = PushDispatcher(store, provider, interval_seconds=interval, metrics=metrics)
    dispatcher.start()
    return dispatcher
