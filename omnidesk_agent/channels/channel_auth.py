"""Inbound authentication helpers for channels whose providers do not sign the body.

Google Chat HTTP endpoints receive ``Authorization: Bearer <JWT>`` signed by
``chat@system.gserviceaccount.com`` with the Cloud project number as audience.
The JWT is verified locally (RS256 against Google's published x509
certificates); the certificate source is injectable so tests never reach the
network.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import re
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, TypeGuard

from omnidesk_agent.channels.verify import header

if TYPE_CHECKING:
    from omnidesk_agent.config import AppConfig, GoogleChatConfig


GOOGLE_CHAT_ISSUER = "chat@system.gserviceaccount.com"
GOOGLE_CHAT_CERTS_URL = "https://www.googleapis.com/service_accounts/v1/metadata/x509/chat@system.gserviceaccount.com"
GOOGLE_CHAT_JWT_LEEWAY_SECONDS = 60
MAX_BEARER_TOKEN_CHARS = 8192
MAX_CERT_RESPONSE_BYTES = 64 * 1024

_B64URL_RE = re.compile(r"[A-Za-z0-9_-]*")
_PROJECT_NUMBER_RE = re.compile(r"[0-9]{1,20}")
_MAX_AGE_RE = re.compile(r"max-age=(\d+)")

# kid -> PEM certificate. ``refresh=True`` asks for a fresh copy (key rotation).
CertFetcher = Callable[..., Mapping[str, str]]


def bearer_token(headers: Mapping[str, str]) -> str:
    scheme, _, token = header(headers, "authorization").strip().partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def secret_equals(provided: str, expected: str) -> bool:
    """Constant-time comparison that never raises on non-ASCII input."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8", "surrogatepass"), expected.encode("utf-8", "surrogatepass"))


def google_chat_requires_jwt(cfg: "GoogleChatConfig", *, production: bool) -> bool:
    if cfg.require_google_jwt is not None:
        return bool(cfg.require_google_jwt)
    return production


def _b64url_decode(segment: str) -> bytes:
    if not _B64URL_RE.fullmatch(segment):
        raise PermissionError("malformed Google Chat bearer token")
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError) as exc:
        raise PermissionError("malformed Google Chat bearer token") from exc


def _json_segment(segment: str) -> dict[str, Any]:
    try:
        value = json.loads(_b64url_decode(segment))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise PermissionError("malformed Google Chat bearer token") from exc
    if not isinstance(value, dict):
        raise PermissionError("malformed Google Chat bearer token")
    return value


def _is_number(value: Any) -> TypeGuard[float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class GoogleCertFetcher:
    """Caches Google's signing certificates (kid -> PEM) and refreshes them off the request path.

    ``verify_request`` runs on the event loop, so a lookup never waits on the
    network: it returns the cached certificates (stale ones for up to
    ``max_stale_seconds``) and, when they are expired or a refresh is asked for
    (an unknown, attacker-chosen ``kid``), starts one background fetch, at most
    once per ``min_refresh_seconds``. With nothing usable cached the lookup
    fails closed. The cache honours ``Cache-Control: max-age`` up to
    ``max_cache_seconds``. ``prefetch`` warms the cache at startup.
    """

    def __init__(
        self,
        url: str = GOOGLE_CHAT_CERTS_URL,
        *,
        timeout_seconds: float = 5.0,
        min_refresh_seconds: float = 60.0,
        max_cache_seconds: float = 3600.0,
        max_stale_seconds: float = 6 * 3600.0,
        http_get: Optional[Callable[[str, float], tuple[bytes, str]]] = None,
        clock: Callable[[], float] = time.monotonic,
        spawn: Optional[Callable[[Callable[[], None]], None]] = None,
    ):
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.min_refresh_seconds = min_refresh_seconds
        self.max_cache_seconds = max_cache_seconds
        self.max_stale_seconds = max_stale_seconds
        self._http_get = http_get or _httpx_get
        self._clock = clock
        self._spawn = spawn or _spawn_daemon_thread
        self._lock = threading.Lock()
        self._certs: dict[str, str] = {}
        self._fetched_at: Optional[float] = None
        self._expires_at = 0.0
        self._last_attempt: Optional[float] = None
        self._refreshing = False

    def __call__(self, *, refresh: bool = False) -> Mapping[str, str]:
        with self._lock:
            fresh = self._fetched_at is not None and self._clock() < self._expires_at
            if fresh and not refresh:
                return dict(self._certs)
        self.prefetch()
        with self._lock:
            if self._fetched_at is not None and self._clock() - self._fetched_at <= self.max_stale_seconds:
                return dict(self._certs)
        raise PermissionError("Google Chat signing certificates are unavailable")

    def prefetch(self) -> None:
        """Start a background refresh unless one is running or one started recently."""
        with self._lock:
            now = self._clock()
            if self._refreshing or (self._last_attempt is not None and now - self._last_attempt < self.min_refresh_seconds):
                return
            self._last_attempt = now
            self._refreshing = True
        try:
            self._spawn(self._refresh)
        except Exception:  # could not start the worker; the next lookup after the throttle window retries
            with self._lock:
                self._refreshing = False

    def _refresh(self) -> None:
        try:
            body, cache_control = self._http_get(self.url, self.timeout_seconds)
            certs: Optional[dict[str, str]] = _parse_cert_response(body)
        except Exception:  # any fetch/parse failure keeps the bounded stale cache
            certs, cache_control = None, ""
        with self._lock:
            self._refreshing = False
            if certs is not None:
                now = self._clock()
                self._certs = certs
                self._fetched_at = now
                self._expires_at = now + _cache_seconds(cache_control, self.max_cache_seconds)


def _spawn_daemon_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="google-chat-cert-refresh", daemon=True).start()


def _cache_seconds(cache_control: str, ceiling: float) -> float:
    match = _MAX_AGE_RE.search(cache_control or "")
    if not match:
        return min(300.0, ceiling)
    return max(0.0, min(float(match.group(1)), ceiling))


def _parse_cert_response(body: bytes) -> dict[str, str]:
    if len(body) > MAX_CERT_RESPONSE_BYTES:
        raise ValueError("certificate response too large")
    data = json.loads(body)
    if not isinstance(data, dict) or not data:
        raise ValueError("certificate response is not a non-empty object")
    certs: dict[str, str] = {}
    for kid, pem in data.items():
        if isinstance(kid, str) and isinstance(pem, str) and "BEGIN CERTIFICATE" in pem:
            certs[kid] = pem
    if not certs:
        raise ValueError("certificate response has no certificates")
    return certs


def _httpx_get(url: str, timeout_seconds: float) -> tuple[bytes, str]:
    from omnidesk_agent.channels.http_client import _offline_mode_enabled

    if _offline_mode_enabled():
        raise RuntimeError("offline mode forbids fetching Google Chat signing certificates")
    import httpx

    response = httpx.get(url, timeout=timeout_seconds, follow_redirects=False)
    response.raise_for_status()
    return response.content, response.headers.get("cache-control", "")


class GoogleChatJwtVerifier:
    """Verifies the bearer JWT Google Chat sends to an HTTP endpoint app."""

    def __init__(
        self,
        project_number: str,
        *,
        cert_fetcher: Optional[CertFetcher] = None,
        clock: Callable[[], float] = time.time,
        leeway_seconds: int = GOOGLE_CHAT_JWT_LEEWAY_SECONDS,
    ):
        if not _PROJECT_NUMBER_RE.fullmatch(str(project_number or "")):
            raise ValueError("Google Chat project_number must be the numeric Cloud project number")
        self.audience = str(project_number)
        self.cert_fetcher: CertFetcher = cert_fetcher or GoogleCertFetcher()
        self.clock = clock
        self.leeway_seconds = leeway_seconds
        self._keys: dict[tuple[str, str], Any] = {}

    def prefetch(self) -> None:
        prefetch = getattr(self.cert_fetcher, "prefetch", None)
        if callable(prefetch):
            prefetch()

    def verify(self, token: str) -> dict[str, Any]:
        if not token or len(token) > MAX_BEARER_TOKEN_CHARS:
            raise PermissionError("malformed Google Chat bearer token")
        parts = token.split(".")
        if len(parts) != 3 or not all(_B64URL_RE.fullmatch(part) for part in parts):
            raise PermissionError("malformed Google Chat bearer token")
        header_b64, payload_b64, signature_b64 = parts
        jwt_header = _json_segment(header_b64)
        if jwt_header.get("alg") != "RS256":
            raise PermissionError("unsupported Google Chat bearer token algorithm")
        kid = jwt_header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise PermissionError("Google Chat bearer token has no key id")
        signature = _b64url_decode(signature_b64)
        public_key = self._public_key(kid)

        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        try:
            public_key.verify(signature, f"{header_b64}.{payload_b64}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature as exc:
            raise PermissionError("invalid Google Chat bearer token signature") from exc

        claims = _json_segment(payload_b64)
        if claims.get("iss") != GOOGLE_CHAT_ISSUER:
            raise PermissionError("unexpected Google Chat bearer token issuer")
        audience = claims.get("aud")
        if not (audience == self.audience or (isinstance(audience, list) and self.audience in audience)):
            raise PermissionError("unexpected Google Chat bearer token audience")
        now = self.clock()
        expires_at = claims.get("exp")
        issued_at = claims.get("iat")
        if not _is_number(expires_at) or now > expires_at + self.leeway_seconds:
            raise PermissionError("expired Google Chat bearer token")
        if not _is_number(issued_at) or issued_at > now + self.leeway_seconds:
            raise PermissionError("Google Chat bearer token is not yet valid")
        not_before = claims.get("nbf")
        if not_before is not None and (not _is_number(not_before) or not_before > now + self.leeway_seconds):
            raise PermissionError("Google Chat bearer token is not yet valid")
        return claims

    def _public_key(self, kid: str):
        pem = self.cert_fetcher(refresh=False).get(kid)
        if pem is None:
            pem = self.cert_fetcher(refresh=True).get(kid)
        if pem is None:
            raise PermissionError("unknown Google Chat signing key")
        cached = self._keys.get((kid, pem))
        if cached is not None:
            return cached

        from cryptography import x509
        from cryptography.hazmat.primitives.asymmetric import rsa

        try:
            public_key = x509.load_pem_x509_certificate(pem.encode("ascii")).public_key()
        except ValueError as exc:
            raise PermissionError("unusable Google Chat signing certificate") from exc
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise PermissionError("unusable Google Chat signing certificate")
        if len(self._keys) > 32:
            self._keys.clear()
        self._keys[(kid, pem)] = public_key
        return public_key


def production_channel_auth_issues(cfg: "AppConfig", env: Mapping[str, str]) -> list[str]:
    """Channel inbound-auth settings that are unsafe in production."""
    issues: list[str] = []
    google_chat = cfg.channels.google_chat
    matrix = cfg.channels.matrix
    x = cfg.channels.x
    if not cfg.gateway.require_webhook_signatures and (google_chat.enabled or matrix.enabled or x.enabled):
        issues.append("gateway.require_webhook_signatures must be true in production while google_chat, matrix or x is enabled")
    if google_chat.enabled:
        if google_chat.allow_query_token:
            issues.append("channels.google_chat.allow_query_token must be false in production; URL tokens end up in access logs")
        if google_chat_requires_jwt(google_chat, production=True) and not google_chat.project_number:
            issues.append("channels.google_chat.project_number must be set in production so the Google-signed bearer token can be verified")
    if matrix.enabled and matrix.allow_query_token:
        issues.append("channels.matrix.allow_query_token must be false in production; send the hook token as Authorization: Bearer")
    if x.enabled and secret_equals(env.get(x.webhook_crc_token_env, ""), env.get(x.webhook_secret_env, "")):
        issues.append(
            "channels.x webhook_crc_token_env and webhook_secret_env must hold different secrets; "
            "the unauthenticated CRC responder would otherwise sign data with the webhook signature key"
        )
    return issues
