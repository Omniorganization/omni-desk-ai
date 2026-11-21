"""Verification of Google-signed OIDC tokens on Cloud Pub/Sub push requests.

An authenticated push subscription sends ``Authorization: Bearer <JWT>``
signed by Google (RS256, ``iss`` accounts.google.com) for the subscription's
service account (``email``, ``email_verified``) and audience (the push
endpoint URL unless configured otherwise). Certificates come from Google's
OAuth2 x509 endpoint through the same off-request-path cache as the Google
Chat certificates.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from typing import Any, Callable, Optional

from omnidesk_agent.channels.channel_auth import CertFetcher, GoogleCertFetcher

GOOGLE_OAUTH2_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
GOOGLE_OIDC_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
GOOGLE_OIDC_LEEWAY_SECONDS = 60
MAX_TOKEN_CHARS = 8192
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]*")


def _decode(segment: str) -> bytes:
    if not _B64URL_RE.fullmatch(segment):
        raise PermissionError("malformed Pub/Sub push bearer token")
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError) as exc:
        raise PermissionError("malformed Pub/Sub push bearer token") from exc


def _json(segment: str) -> dict[str, Any]:
    try:
        value = json.loads(_decode(segment))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise PermissionError("malformed Pub/Sub push bearer token") from exc
    if not isinstance(value, dict):
        raise PermissionError("malformed Pub/Sub push bearer token")
    return value


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class GooglePushJwtVerifier:
    def __init__(
        self,
        audience: str,
        service_account_email: str,
        *,
        cert_fetcher: Optional[CertFetcher] = None,
        clock: Callable[[], float] = time.time,
    ):
        if not audience or not service_account_email:
            raise ValueError("Pub/Sub push verification needs the audience and the push service account email")
        self.audience = audience
        self.service_account_email = service_account_email.lower()
        self.cert_fetcher: CertFetcher = cert_fetcher or GoogleCertFetcher(GOOGLE_OAUTH2_CERTS_URL)
        self.clock = clock
        self._keys: dict[tuple[str, str], Any] = {}

    def prefetch(self) -> None:
        prefetch = getattr(self.cert_fetcher, "prefetch", None)
        if callable(prefetch):
            prefetch()

    def verify(self, token: str) -> dict[str, Any]:
        if not token or len(token) > MAX_TOKEN_CHARS:
            raise PermissionError("missing or malformed Pub/Sub push bearer token")
        parts = token.split(".")
        if len(parts) != 3 or not all(_B64URL_RE.fullmatch(part) for part in parts):
            raise PermissionError("malformed Pub/Sub push bearer token")
        header_b64, payload_b64, signature_b64 = parts
        header = _json(header_b64)
        if header.get("alg") != "RS256":
            raise PermissionError("unsupported Pub/Sub push bearer token algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise PermissionError("Pub/Sub push bearer token has no key id")
        public_key = self._public_key(kid)

        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        try:
            public_key.verify(_decode(signature_b64), f"{header_b64}.{payload_b64}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature as exc:
            raise PermissionError("invalid Pub/Sub push bearer token signature") from exc

        claims = _json(payload_b64)
        if claims.get("iss") not in GOOGLE_OIDC_ISSUERS:
            raise PermissionError("unexpected Pub/Sub push bearer token issuer")
        audience = claims.get("aud")
        if not (audience == self.audience or (isinstance(audience, list) and self.audience in audience)):
            raise PermissionError("unexpected Pub/Sub push bearer token audience")
        if str(claims.get("email") or "").lower() != self.service_account_email or claims.get("email_verified") is not True:
            raise PermissionError("Pub/Sub push bearer token is not for the configured service account")
        now = self.clock()
        if not _number(claims.get("exp")) or now > claims["exp"] + GOOGLE_OIDC_LEEWAY_SECONDS:
            raise PermissionError("expired Pub/Sub push bearer token")
        if not _number(claims.get("iat")) or claims["iat"] > now + GOOGLE_OIDC_LEEWAY_SECONDS:
            raise PermissionError("Pub/Sub push bearer token is not yet valid")
        return claims

    def _public_key(self, kid: str):
        pem = self.cert_fetcher(refresh=False).get(kid)
        if pem is None:
            pem = self.cert_fetcher(refresh=True).get(kid)
        if not isinstance(pem, str):
            raise PermissionError("unknown Google signing key")
        cached = self._keys.get((kid, pem))
        if cached is not None:
            return cached
        from cryptography import x509
        from cryptography.hazmat.primitives.asymmetric import rsa

        try:
            public_key = x509.load_pem_x509_certificate(pem.encode("ascii")).public_key()
        except ValueError as exc:
            raise PermissionError("unusable Google signing certificate") from exc
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise PermissionError("unusable Google signing certificate")
        if len(self._keys) > 32:
            self._keys.clear()
        self._keys[(kid, pem)] = public_key
        return public_key
