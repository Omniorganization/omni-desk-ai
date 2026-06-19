from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from typing import Optional


class EncryptionConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class EncryptionProvider:
    """Small envelope encryption wrapper for sensitive local runtime fields.

    The implementation uses Fernet from `cryptography` when enabled. The key may
    be either a valid Fernet key or any high-entropy deployment secret, which is
    SHA-256 derived into a Fernet key for operator ergonomics.
    """

    enabled: bool = False
    key_id: str = "disabled"
    _fernet: object | None = None

    PREFIX = "enc:v1:"

    @classmethod
    def disabled(cls) -> "EncryptionProvider":
        return cls(enabled=False, key_id="disabled", _fernet=None)

    @classmethod
    def from_env(cls, env_name: str, *, required: bool = False, key_id: Optional[str] = None) -> "EncryptionProvider":
        secret = os.getenv(env_name, "")
        if not secret:
            if required:
                raise EncryptionConfigurationError(f"encryption key is not configured: {env_name}")
            return cls.disabled()
        return cls.from_secret(secret, key_id=key_id or env_name)

    @classmethod
    def from_secret(cls, secret: str, *, key_id: str = "local") -> "EncryptionProvider":
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise EncryptionConfigurationError("cryptography is required for encrypt_at_rest") from exc

        raw = secret.encode("utf-8")
        try:
            # Accept a deployment-provided Fernet key as-is.
            Fernet(raw)
            key = raw
        except Exception:
            key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
        return cls(enabled=True, key_id=key_id, _fernet=Fernet(key))

    def encrypt_text(self, value: Optional[str]) -> Optional[str]:
        if value is None or not self.enabled:
            return value
        if value.startswith(self.PREFIX):
            return value
        token = self._fernet.encrypt(value.encode("utf-8"))  # type: ignore[union-attr]
        return self.PREFIX + self.key_id + ":" + token.decode("ascii")

    def decrypt_text(self, value: Optional[str]) -> Optional[str]:
        if value is None or not isinstance(value, str) or not value.startswith(self.PREFIX):
            return value
        if not self.enabled:
            raise EncryptionConfigurationError("encrypted data found but encryption provider is disabled")
        try:
            _prefix, _version, _key_id, token = value.split(":", 3)
        except ValueError as exc:
            raise EncryptionConfigurationError("malformed encrypted value") from exc
        return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")  # type: ignore[union-attr]

    def maybe_decrypt_mapping(self, row: dict, fields: list[str]) -> dict:
        out = dict(row)
        for field in fields:
            if field in out:
                out[field] = self.decrypt_text(out[field])
        return out
