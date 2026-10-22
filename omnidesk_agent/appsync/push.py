from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
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
                raise RuntimeError(f"FCM OAuth token request failed: HTTP {response.status_code}")
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
                status = response.json().get("error", {}).get("status", "")
            except ValueError:
                status = ""
            raise RuntimeError(f"FCM send failed: HTTP {response.status_code} {status}".strip())
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
            data = {"push_id": str(item.get("push_id") or ""), "related_id": str(item.get("related_id") or ""), "audience": str(item.get("audience") or "")}
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
