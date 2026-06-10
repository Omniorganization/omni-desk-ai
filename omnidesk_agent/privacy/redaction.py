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
    TOKEN_RE = re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*['\"]?[^'\"\s,}]+")
    PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)

    def redact_text(self, text: str) -> str:
        text = self.PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", text)
        text = self.TOKEN_RE.sub(lambda m: m.group(1) + "=[REDACTED_SECRET]", text)
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
                if str(k).lower() in {"token", "secret", "password", "api_key", "authorization", "cookie"}:
                    out[k] = "[REDACTED_SECRET]"
                else:
                    out[k] = self.redact_obj(v)
            return out
        return value
