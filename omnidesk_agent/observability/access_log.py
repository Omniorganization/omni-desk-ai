"""Keep credentials that arrive in URL query strings out of server logs.

uvicorn logs the full request target (path and query) on HTTP access lines and
on WebSocket accept/close lines. Some callers can only be configured with a URL
(legacy ``?token=`` hooks, Meta ``hub.verify_token``) or still send query
credentials, so credential-like parameter values are replaced before any
handler formats the record.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any
from urllib.parse import unquote_plus

REDACTED = "[REDACTED]"
_SENSITIVE_QUERY_NAME_RE = re.compile(r"token|secret|passw|signature|credential|api[_-]?key|^key$|^code$|^auth|session", re.IGNORECASE)


def redact_query_secrets(target: str) -> str:
    path, sep, query = target.partition("?")
    if not sep or not query:
        return target
    redacted = []
    for pair in query.split("&"):
        name, eq, _value = pair.partition("=")
        if eq and _SENSITIVE_QUERY_NAME_RE.search(unquote_plus(name)):
            redacted.append(f"{name}={REDACTED}")
        else:
            redacted.append(pair)
    return f"{path}?{'&'.join(redacted)}"


class QuerySecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(redact_query_secrets(arg) if isinstance(arg, str) and "?" in arg else arg for arg in record.args)
        return True


def uvicorn_log_config() -> dict[str, Any]:
    """uvicorn's default logging config with query secrets redacted on every handler."""
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["redact_query_secrets"] = {"()": f"{__name__}.QuerySecretRedactingFilter"}
    for handler in config.get("handlers", {}).values():
        handler["filters"] = [*handler.get("filters", []), "redact_query_secrets"]
    return config
