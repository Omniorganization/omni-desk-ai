"""Microsoft Bot Framework (Teams) authentication.

Inbound: the Bot Connector sends ``Authorization: Bearer <JWT>``. It is RS256,
issued by ``https://api.botframework.com`` for the bot's Microsoft App ID,
signed with a key from the Bot Framework OpenID metadata, endorsed for the
activity's ``channelId``, and carries a ``serviceUrl`` claim equal to the
activity's ``serviceUrl``. Keys are fetched off the request path (same cache
as the Google Chat certificates), so verification never waits on the network.

Outbound: an OAuth 2.0 client-credentials token for
``https://api.botframework.com/.default`` from the bot's tenant (or
``botframework.com`` for legacy multi-tenant bots), cached until shortly
before it expires.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Mapping, Optional

from omnidesk_agent.channels.channel_auth import CertFetcher, GoogleCertFetcher, MAX_BEARER_TOKEN_CHARS, _B64URL_RE, _b64url_decode, _httpx_get, _is_number, _json_segment

BOT_FRAMEWORK_OPENID_CONFIGURATION_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
BOT_FRAMEWORK_ISSUER = "https://api.botframework.com"
BOT_FRAMEWORK_SCOPE = "https://api.botframework.com/.default"
BOT_FRAMEWORK_CLOCK_SKEW_SECONDS = 300
_JWKS_HOST_PREFIX = "https://login.botframework.com/"
_TENANT_RE = re.compile(r"[A-Za-z0-9.-]{1,128}")
_LABEL = "Teams"


def _bot_framework_jwks_get(url: str, timeout_seconds: float) -> tuple[bytes, str]:
    """Fetch the OpenID metadata, then the JWKS it names (pinned to login.botframework.com)."""
    metadata_body, _ = _httpx_get(url, timeout_seconds)
    metadata = json.loads(metadata_body)
    jwks_uri = metadata.get("jwks_uri") if isinstance(metadata, dict) else None
    if not isinstance(jwks_uri, str) or not jwks_uri.startswith(_JWKS_HOST_PREFIX):
        raise ValueError("unexpected Bot Framework jwks_uri")
    return _httpx_get(jwks_uri, timeout_seconds)


def parse_jwks(body: bytes) -> dict[str, dict[str, Any]]:
    data = json.loads(body)
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, list):
        raise ValueError("JWKS response has no keys")
    parsed: dict[str, dict[str, Any]] = {}
    for key in keys:
        if isinstance(key, dict) and isinstance(key.get("kid"), str) and key.get("kty") == "RSA" and isinstance(key.get("n"), str) and isinstance(key.get("e"), str):
            parsed[key["kid"]] = key
    if not parsed:
        raise ValueError("JWKS response has no RSA keys")
    return parsed


def bot_framework_key_fetcher() -> GoogleCertFetcher:
    return GoogleCertFetcher(
        BOT_FRAMEWORK_OPENID_CONFIGURATION_URL,
        http_get=_bot_framework_jwks_get,
        parse=parse_jwks,
        # The docs ask for a refresh at least every 24 hours; the cache already
        # refreshes hourly at most and on any unknown key id.
        unavailable_message="Bot Framework signing keys are unavailable",
    )


def _b64url_int(value: str) -> int:
    return int.from_bytes(_b64url_decode(value, _LABEL), "big")


class BotFrameworkJwtVerifier:
    def __init__(self, app_id: str, *, key_fetcher: Optional[CertFetcher] = None, clock: Callable[[], float] = time.time):
        if not app_id:
            raise ValueError("Teams app id is required for Bot Framework token validation")
        self.app_id = app_id
        self.key_fetcher: CertFetcher = key_fetcher or bot_framework_key_fetcher()
        self.clock = clock
        self._keys: dict[tuple[str, str, str], Any] = {}

    def prefetch(self) -> None:
        prefetch = getattr(self.key_fetcher, "prefetch", None)
        if callable(prefetch):
            prefetch()

    def verify(self, token: str, *, service_url: str, channel_id: str) -> dict[str, Any]:
        if not token or len(token) > MAX_BEARER_TOKEN_CHARS:
            raise PermissionError("malformed Teams bearer token")
        parts = token.split(".")
        if len(parts) != 3 or not all(_B64URL_RE.fullmatch(part) for part in parts):
            raise PermissionError("malformed Teams bearer token")
        header_b64, payload_b64, signature_b64 = parts
        jwt_header = _json_segment(header_b64, _LABEL)
        if jwt_header.get("alg") != "RS256":
            raise PermissionError("unsupported Teams bearer token algorithm")
        kid = jwt_header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise PermissionError("Teams bearer token has no key id")
        jwk = self._jwk(kid)
        public_key = self._public_key(kid, jwk)

        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        try:
            public_key.verify(_b64url_decode(signature_b64, _LABEL), f"{header_b64}.{payload_b64}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature as exc:
            raise PermissionError("invalid Teams bearer token signature") from exc

        claims = _json_segment(payload_b64, _LABEL)
        if claims.get("iss") != BOT_FRAMEWORK_ISSUER:
            raise PermissionError("unexpected Teams bearer token issuer")
        audience = claims.get("aud")
        if not (audience == self.app_id or (isinstance(audience, list) and self.app_id in audience)):
            raise PermissionError("unexpected Teams bearer token audience")
        now = self.clock()
        expires_at = claims.get("exp")
        if not _is_number(expires_at) or now > expires_at + BOT_FRAMEWORK_CLOCK_SKEW_SECONDS:
            raise PermissionError("expired Teams bearer token")
        not_before = claims.get("nbf")
        if not_before is not None and (not _is_number(not_before) or not_before > now + BOT_FRAMEWORK_CLOCK_SKEW_SECONDS):
            raise PermissionError("Teams bearer token is not yet valid")
        claimed_service_url = claims.get("serviceUrl") or claims.get("serviceurl")
        if not isinstance(claimed_service_url, str) or not service_url or claimed_service_url.rstrip("/") != str(service_url).rstrip("/"):
            raise PermissionError("Teams bearer token serviceUrl does not match the activity")
        endorsements = jwk.get("endorsements")
        if not isinstance(endorsements, list) or channel_id not in endorsements:
            raise PermissionError("Teams signing key is not endorsed for this channel")
        return claims

    def _jwk(self, kid: str) -> Mapping[str, Any]:
        jwk = self.key_fetcher(refresh=False).get(kid)
        if jwk is None:
            jwk = self.key_fetcher(refresh=True).get(kid)
        if not isinstance(jwk, Mapping):
            raise PermissionError("unknown Teams signing key")
        return jwk

    def _public_key(self, kid: str, jwk: Mapping[str, Any]):
        cache_key = (kid, str(jwk.get("n")), str(jwk.get("e")))
        cached = self._keys.get(cache_key)
        if cached is not None:
            return cached
        from cryptography.hazmat.primitives.asymmetric import rsa

        try:
            public_key = rsa.RSAPublicNumbers(_b64url_int(str(jwk["e"])), _b64url_int(str(jwk["n"]))).public_key()
        except (KeyError, ValueError) as exc:
            raise PermissionError("unusable Teams signing key") from exc
        if len(self._keys) > 32:
            self._keys.clear()
        self._keys[cache_key] = public_key
        return public_key


def token_endpoint(tenant_id: str) -> str:
    tenant = tenant_id or "botframework.com"
    if not _TENANT_RE.fullmatch(tenant):
        raise ValueError("Teams tenant id is not a tenant id or domain")
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

