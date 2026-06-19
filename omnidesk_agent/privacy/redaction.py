from __future__ import annotations

import re
from typing import Any


class MemoryPrivacyFilter:
    """Conservative local redactor for long-term memory.

    This is deliberately deterministic and local. It reduces exposure of emails,
    phone numbers, API keys and common private-key blocks before task traces are
    stored in SQLite.
    """

    EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
    PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s\-()]{7,}\d)(?!\w)")
    CREDENTIAL_VALUE_RE = re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|secret|client[_-]?secret|password|session[_-]?id)\b"
        r"(\s*[:=]\s*)(['\"]?)[^'\"\s,}&]+"
    )
    HEADER_SECRET_RE = re.compile(r"(?i)\b(authorization|cookie)\b(\s*[:=]\s*)(['\"]?)(?:bearer|basic)?\s*[^'\"\s,}]+")
    AUTH_SCHEME_RE = re.compile(r"(?i)\b(bearer|basic)\s+[a-z0-9._~+/=-]+")
    SECRET_DICT_KEY_RE = re.compile(
        r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|secret|password|authorization|cookie|private[_-]?key)"
    )
    PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)

    @staticmethod
    def _replace_secret_value(match: re.Match[str]) -> str:
        quote = match.group(3) or ""
        return f"{match.group(1)}{match.group(2)}{quote}[REDACTED_SECRET]{quote}"

    def redact_text(self, text: str) -> str:
        text = self.PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", text)
        text = self.HEADER_SECRET_RE.sub(self._replace_secret_value, text)
        text = self.CREDENTIAL_VALUE_RE.sub(self._replace_secret_value, text)
        text = self.AUTH_SCHEME_RE.sub(lambda m: m.group(1) + " [REDACTED_SECRET]", text)
        text = self.EMAIL_RE.sub("[REDACTED_EMAIL]", text)
        text = self.PHONE_RE.sub("[REDACTED_PHONE]", text)
        return text

    def redact_obj(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, list):
            return [self.redact_obj(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.redact_obj(v) for v in value)
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                key = str(k).lower()
                if key == "token" or self.SECRET_DICT_KEY_RE.search(key):
                    out[k] = "[REDACTED_SECRET]"
                else:
                    out[k] = self.redact_obj(v)
            return out
        return value
