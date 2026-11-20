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
    # Credentials carried in URL query strings (Gemini ?key=, signed URLs,
    # OAuth callbacks). Bare "key=" is only treated as a secret inside a query.
    URL_QUERY_SECRET_RE = re.compile(
        r"(?i)([?&;](?:key|api[_-]?key|apikey|x-goog-api-key|access[_-]?token|token|auth|code|sig|signature|secret|client[_-]?secret|password)=)"
        r"[^&#\s'\"<>]+"
    )
    # Keys may end in "-", so delimit with lookarounds rather than \b.
    GOOGLE_API_KEY_RE = re.compile(r"(?<![0-9A-Za-z_-])AIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])")
    TELEGRAM_BOT_TOKEN_RE = re.compile(r"(?i)(/bot)\d{5,}:[A-Za-z0-9_-]{30,}")
    # Self-identifying token formats, recognised by their prefix wherever they appear
    # (typed text, file bodies, messages): GitHub, Slack, AWS access key ids, Stripe,
    # Anthropic/OpenAI-style "sk-" keys, Google OAuth tokens, and JWTs.
    PREFIXED_TOKEN_RE = re.compile(
        r"(?<![0-9A-Za-z_-])(?:"
        r"gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,}"
        r"|xox[abprs]-[A-Za-z0-9-]{10,}"
        r"|(?:AKIA|ASIA)[0-9A-Z]{16}"
        r"|(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}"
        r"|sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"
        r"|ya29\.[0-9A-Za-z_-]{20,}"
        r"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
        r")(?![0-9A-Za-z_-])"
    )
    STRICT_AUTH_SCHEME_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}")
    URL_USERINFO_RE = re.compile(r"(?i)\b((?:https?|wss?|ftp|postgres(?:ql)?|mysql|redis|amqp)://[^\s:/@'\"<>]+:)[^\s@/'\"<>]+(@)")

    @staticmethod
    def _replace_secret_value(match: re.Match[str]) -> str:
        quote = match.group(3) or ""
        return f"{match.group(1)}{match.group(2)}{quote}[REDACTED_SECRET]{quote}"

    def redact_secrets(self, text: str) -> str:
        """Redact credentials only; leaves contact details and other text intact."""
        text = self.PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", text)
        text = self.PREFIXED_TOKEN_RE.sub("[REDACTED_SECRET]", text)
        text = self.URL_USERINFO_RE.sub(lambda m: m.group(1) + "[REDACTED_SECRET]" + m.group(2), text)
        text = self.URL_QUERY_SECRET_RE.sub(lambda m: m.group(1) + "[REDACTED_SECRET]", text)
        text = self.TELEGRAM_BOT_TOKEN_RE.sub(lambda m: m.group(1) + "[REDACTED_SECRET]", text)
        text = self.GOOGLE_API_KEY_RE.sub("[REDACTED_SECRET]", text)
        text = self.HEADER_SECRET_RE.sub(self._replace_secret_value, text)
        text = self.CREDENTIAL_VALUE_RE.sub(self._replace_secret_value, text)
        text = self.AUTH_SCHEME_RE.sub(lambda m: m.group(1) + " [REDACTED_SECRET]", text)
        return text

    def redact_known_credentials(self, text: str) -> str:
        """Redact only self-identifying credential formats.

        Unlike redact_secrets this skips the generic ``token: value`` and header
        patterns, which also match identifiers such as ``token:OMNIDESK_OWNER_TOKEN``;
        records that hold identities (approvals, audit entries) must keep those intact.
        """
        text = self.PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", text)
        text = self.PREFIXED_TOKEN_RE.sub("[REDACTED_SECRET]", text)
        text = self.URL_USERINFO_RE.sub(lambda m: m.group(1) + "[REDACTED_SECRET]" + m.group(2), text)
        text = self.URL_QUERY_SECRET_RE.sub(lambda m: m.group(1) + "[REDACTED_SECRET]", text)
        text = self.TELEGRAM_BOT_TOKEN_RE.sub(lambda m: m.group(1) + "[REDACTED_SECRET]", text)
        text = self.GOOGLE_API_KEY_RE.sub("[REDACTED_SECRET]", text)
        text = self.STRICT_AUTH_SCHEME_RE.sub(lambda m: m.group(1) + " [REDACTED_SECRET]", text)
        return text

    def redact_text(self, text: str) -> str:
        text = self.redact_secrets(text)
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


_URL_RE = re.compile(r"(?i)\b((?:https?|wss?)://)(?:[^\s/?#@'\"<>]*@)?([^\s?#'\"<>]*)(?:\?[^\s#'\"<>]*)?(?:#[^\s'\"<>]*)?")


def strip_url_query(text: str) -> str:
    """Drop userinfo, query strings and fragments from every URL in text.

    HTTP client errors quote the full request URL; whatever a provider put in
    the query (keys, signatures, tokens) must not reach logs or stored errors.
    """
    return _URL_RE.sub(lambda m: m.group(1) + m.group(2), text)


_SECRET_FILTER = MemoryPrivacyFilter()


def redact_secret_values(value: Any) -> Any:
    """Redact self-identifying credentials inside string values, recursively.

    Used for approval records and audit entries: the approver keeps seeing the
    content they approve, minus credentials that happen to appear in it, and
    identities such as ``token:OMNIDESK_OWNER_TOKEN`` stay intact.
    """
    if isinstance(value, str):
        return _SECRET_FILTER.redact_known_credentials(value)
    if isinstance(value, dict):
        return {k: redact_secret_values(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_secret_values(v) for v in value]
    return value
