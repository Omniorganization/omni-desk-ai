from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from omnidesk_agent.channels.http_client import ChannelHttpError, _redact_error_text


@dataclass(frozen=True)
class ChannelErrorInfo:
    category: str
    retryable: bool
    dead_letter_now: bool
    alert: bool
    status_code: Optional[int] = None


# category -> (retryable, dead_letter_now, alert). The outbound dispatcher
# treats timeout/network_error/provider_5xx as possibly delivered (ambiguous)
# for providers without native idempotency; not_sent and rate_limit mean the
# provider did not act on the request, so a later retry cannot duplicate it.
CATEGORY_POLICY: dict[str, tuple[bool, bool, bool]] = {
    "rate_limit": (True, False, False),
    "provider_5xx": (True, False, False),
    "timeout": (True, False, False),
    "network_error": (True, False, False),
    "not_sent": (True, False, False),
    "auth_error": (False, True, True),
    "bad_request": (False, True, False),
    "recipient_unavailable": (False, True, False),
    "unsupported_payload": (False, True, False),
    "config_error": (False, True, True),
    "policy_error": (False, True, True),
    "provider_rejected": (False, True, True),
    "unknown": (True, False, False),
}


def _info(category: str, status: Optional[int]) -> ChannelErrorInfo:
    retryable, dead_letter_now, alert = CATEGORY_POLICY.get(category, CATEGORY_POLICY["unknown"])
    return ChannelErrorInfo(category, retryable, dead_letter_now, alert, status)


def _status_code(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return int(value) if isinstance(value, int) else None


# --- provider error vocabularies -------------------------------------------------
# Meta Graph (WhatsApp Cloud, Messenger, Instagram): error.code / error_subcode.
_GRAPH_RATE_LIMIT = {4, 17, 32, 613, 80001, 80002, 80004, 80005, 80006, 80008, 80014, 130429, 131048, 131056, 133016}
_GRAPH_AUTH = {0, 102, 190}
_GRAPH_POLICY = {10, 200, 368, 470, 131031, 131042, 131047, 131049, 131050, 2018278}
_GRAPH_RECIPIENT = {551, 131021, 131026, 1545041, 2018001, 2018108}
# 131030: recipient not in the test number's allowed list (add it in API Setup).
_GRAPH_CONFIG = {131030}
_GRAPH_PAYLOAD = {131051, 131052, 131053}
_GRAPH_TRANSIENT = {1, 2, 131000, 131016}

# Telegram Bot API descriptions for a blocked/unreachable recipient (HTTP 403).
_TELEGRAM_RECIPIENT_MARKERS = ("blocked by the user", "user is deactivated", "bot was kicked", "can't initiate conversation", "not a member", "chat not found", "user not found")

# WeChat Official Account errcodes (returned inside HTTP 200).
_WECHAT_CODES: dict[int, str] = {
    -1: "provider_5xx",
    40001: "auth_error", 40014: "auth_error", 42001: "auth_error", 40013: "config_error", 40125: "config_error", 40164: "config_error", 48001: "config_error",
    40003: "bad_request", 43004: "recipient_unavailable", 45015: "policy_error", 45047: "policy_error",
    45009: "rate_limit", 45011: "rate_limit", 45002: "unsupported_payload", 40008: "unsupported_payload", 44002: "bad_request",
}
# DingTalk custom-robot errcodes (inside HTTP 200). 310000 is a failed
# security check (keyword, timestamp, sign or IP allowlist); -1 "system busy"
# may still have posted, so it is treated as ambiguous.
_DINGTALK_CODES: dict[int, str] = {
    -1: "provider_5xx", 410100: "rate_limit", 130101: "rate_limit",
    310000: "config_error", 400101: "config_error", 400102: "config_error", 400106: "config_error", 300001: "config_error",
    400013: "recipient_unavailable",
    430101: "policy_error", 430102: "policy_error", 430103: "policy_error", 430104: "policy_error",
}
# Lark / Feishu ``code``.
_LARK_CODES: dict[int, str] = {
    99991400: "rate_limit", 230020: "rate_limit", 11232: "rate_limit",
    99991661: "auth_error", 99991663: "auth_error", 99991668: "auth_error", 99991672: "config_error", 10003: "config_error", 10014: "config_error",
    230001: "bad_request", 99992402: "bad_request", 230002: "recipient_unavailable", 230013: "recipient_unavailable",
    230006: "config_error", 230025: "unsupported_payload", 230099: "provider_5xx",
}
# Slack Web API ``error`` strings (ok:false inside HTTP 200).
_SLACK_ERRORS: dict[str, str] = {
    "ratelimited": "rate_limit",
    "invalid_auth": "auth_error", "not_authed": "auth_error", "token_revoked": "auth_error", "token_expired": "auth_error", "account_inactive": "auth_error",
    "missing_scope": "config_error", "restricted_action": "policy_error", "not_allowed_token_type": "config_error",
    "channel_not_found": "bad_request", "user_not_found": "bad_request", "not_in_channel": "recipient_unavailable", "is_archived": "recipient_unavailable",
    "msg_too_long": "unsupported_payload", "no_text": "unsupported_payload", "invalid_blocks": "unsupported_payload",
    # Slack documents that part of the operation may have succeeded.
    "internal_error": "provider_5xx", "fatal_error": "provider_5xx",
    # The POST body never arrived intact, so nothing was posted.
    "request_timeout": "not_sent", "service_unavailable": "not_sent",
}
_DISCORD_CODES: dict[int, str] = {
    0: "auth_error", 40001: "auth_error", 50001: "config_error", 50013: "config_error",
    50007: "recipient_unavailable", 10003: "bad_request", 10013: "bad_request", 50035: "bad_request", 40005: "unsupported_payload",
}
_MATRIX_ERRCODES: dict[str, str] = {
    "M_LIMIT_EXCEEDED": "rate_limit", "M_UNKNOWN_TOKEN": "auth_error", "M_MISSING_TOKEN": "auth_error",
    "M_FORBIDDEN": "recipient_unavailable", "M_NOT_FOUND": "bad_request", "M_TOO_LARGE": "unsupported_payload", "M_BAD_JSON": "bad_request",
}
_GOOGLE_STATUS: dict[str, str] = {
    "RESOURCE_EXHAUSTED": "rate_limit", "UNAUTHENTICATED": "auth_error", "PERMISSION_DENIED": "config_error",
    "NOT_FOUND": "bad_request", "INVALID_ARGUMENT": "bad_request", "FAILED_PRECONDITION": "policy_error",
}
_TEAMS_CODES: dict[str, str] = {
    "Unauthorized": "auth_error", "Forbidden": "config_error", "ConversationNotFound": "recipient_unavailable",
    "BotNotInConversationRoster": "recipient_unavailable", "ConversationBlocked": "recipient_unavailable",
    "BadArgument": "bad_request", "BadSyntax": "bad_request", "MissingProperty": "bad_request", "MessageSizeTooBig": "unsupported_payload",
}


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def provider_category(channel: str, data: Any, status: Optional[int] = None) -> Optional[str]:
    """Map a provider's own error vocabulary to an outbox category.

    ``data`` is the parsed response body: either an HTTP-200 envelope that
    carries an error (WeChat, DingTalk, Lark, Slack) or a 4xx/5xx error body.
    Returns None when the body says nothing the HTTP status does not.
    """
    if not isinstance(data, dict):
        return None
    if channel in {"whatsapp_cloud", "whatsapp", "meta_graph"}:
        error = data.get("error")
        if not isinstance(error, dict):
            return None
        code, subcode = _int(error.get("code")), _int(error.get("error_subcode"))
        for value in (subcode, code):
            if value in _GRAPH_CONFIG:
                return "config_error"
            if value in _GRAPH_RATE_LIMIT:
                return "rate_limit"
            if value in _GRAPH_RECIPIENT:
                return "recipient_unavailable"
            if value in _GRAPH_POLICY:
                return "policy_error"
            if value in _GRAPH_PAYLOAD:
                return "unsupported_payload"
        if code in _GRAPH_AUTH:
            return "auth_error"
        if code in _GRAPH_TRANSIENT:
            return "provider_5xx" if (status or 0) >= 500 else "rate_limit"
        if code is not None and 132000 <= code <= 132099:
            return "policy_error"  # template errors
        return None
    if channel == "telegram":
        if data.get("ok") is not False:
            return None
        code = _int(data.get("error_code"))
        description = str(data.get("description") or "").lower()
        if code == 429:
            return "rate_limit"
        if code == 401:
            return "auth_error"
        if code == 404:
            return "config_error"
        if code == 409:
            return "config_error"
        if any(marker in description for marker in _TELEGRAM_RECIPIENT_MARKERS):
            return "recipient_unavailable"
        if "too long" in description or "message text is empty" in description:
            return "unsupported_payload"
        if code == 400:
            return "bad_request"
        if code == 403:
            return "recipient_unavailable"
        return None
    if channel == "wechat_official":
        code = _int(data.get("errcode"))
        if code in (None, 0):
            return None
        return _WECHAT_CODES.get(code, "provider_rejected")
    if channel == "dingtalk":
        code = _int(data.get("errcode"))
        if code not in (None, 0):
            return _DINGTALK_CODES.get(code, "provider_rejected")
        text_code = str(data.get("code") or "")
        if not text_code:
            return None
        lowered = text_code.lower()
        if "throttl" in lowered or "limit" in lowered:
            return "rate_limit"
        if "authentication" in lowered or "accesstoken" in lowered or "token" in lowered:
            return "auth_error"
        if "forbidden" in lowered or "nopermission" in lowered:
            return "config_error"
        if "param" in lowered or "invalid" in lowered:
            return "bad_request"
        return "provider_rejected"
    if channel in {"lark", "feishu"}:
        code = _int(data.get("code"))
        if code in (None, 0):
            return None
        return _LARK_CODES.get(code, "provider_rejected")
    if channel == "slack":
        if data.get("ok") is not False:
            return None
        return _SLACK_ERRORS.get(str(data.get("error") or ""), "provider_rejected")
    if channel == "discord":
        code = _int(data.get("code"))
        if code is None:
            return None
        return _DISCORD_CODES.get(code)
    if channel == "matrix":
        return _MATRIX_ERRCODES.get(str(data.get("errcode") or ""))
    if channel == "google_chat":
        error = data.get("error")
        if isinstance(error, dict):
            return _GOOGLE_STATUS.get(str(error.get("status") or ""))
        return None
    if channel in {"microsoft_teams", "teams"}:
        error = data.get("error")
        if isinstance(error, dict):
            return _TEAMS_CODES.get(str(error.get("code") or ""))
        return None
    if channel == "x":
        detail = str(data.get("detail") or "").lower()
        if "duplicate" in detail:
            return "policy_error"
        return None
    return None


def _status_category(status: int) -> Optional[str]:
    if status == 429:
        return "rate_limit"
    if 500 <= status <= 599:
        return "provider_5xx"
    if status in {408, 425}:
        return "not_sent"
    if status in {401, 403}:
        return "auth_error"
    if status == 413:
        return "unsupported_payload"
    if status in {400, 404, 422}:
        return "bad_request"
    if 400 <= status <= 499:
        return "provider_rejected"
    return None


# Providers that report failures inside an HTTP 200 body.
IN_BAND_ERROR_CHANNELS = frozenset({"telegram", "wechat_official", "dingtalk", "lark", "feishu", "slack"})


def ensure_provider_success(channel: str, result: Any) -> None:
    """Raise if an HTTP-success response carries the provider's error envelope.

    WeChat, DingTalk and Lark answer most failures with HTTP 200 and a non-zero
    code, and Slack with ``ok: false``; such a send was not delivered and must
    not be recorded as sent.
    """
    if channel not in IN_BAND_ERROR_CHANNELS:
        return
    data = getattr(result, "data", None)
    category = provider_category(channel, data, getattr(result, "status_code", None))
    if category is None:
        return
    code = None
    message = ""
    if isinstance(data, dict):
        for key in ("errcode", "code", "error_code", "error"):
            if data.get(key) not in (None, ""):
                code = str(data.get(key))
                break
        message = str(data.get("errmsg") or data.get("msg") or data.get("description") or data.get("message") or "")
    raise ChannelHttpError(
        f"{channel} provider rejected the request: code={code} {_redact_error_text(message)[:300]}".rstrip(),
        status_code=getattr(result, "status_code", None),
        request_id=getattr(result, "request_id", None),
        channel=channel,
        request_sent=True,
        category=category,
        provider_code=code,
    )


def classify_channel_error(exc: BaseException) -> ChannelErrorInfo:
    status = _status_code(exc)
    explicit = getattr(exc, "category", None)
    if isinstance(explicit, str) and explicit in CATEGORY_POLICY:
        return _info(explicit, status)
    channel = getattr(exc, "channel", None)
    if isinstance(channel, str):
        mapped = provider_category(channel, getattr(exc, "response_json", None), status)
        if mapped is not None:
            # A 5xx stays ambiguous whatever the body says, unless the body
            # names a rate limit (the provider did not act on the request).
            if status is not None and status >= 500 and mapped not in {"rate_limit", "provider_5xx"}:
                return _info("provider_5xx", status)
            return _info(mapped, status)
    if status is not None:
        by_status = _status_category(status)
        if by_status is not None:
            return _info(by_status, status)
    text = str(exc).lower()
    if "rate limit" in text or "too many requests" in text:
        return _info("rate_limit", status)
    if "timeout" in text or "timed out" in text:
        return _info("timeout", status)
    if "network error" in text or "connection reset" in text or "transport error" in text:
        return _info("network_error", status)
    if "unauthorized" in text or "forbidden" in text:
        return _info("auth_error", status)
    if "invalid recipient" in text or "recipient invalid" in text:
        return _info("bad_request", status)
    if "unsupported" in text:
        return _info("unsupported_payload", status)
    if "unknown channel" in text or "adapter" in text:
        return _info("config_error", status)
    if "template" in text or "session window" in text or "policy" in text:
        return _info("policy_error", status)
    return _info("unknown", status)


def before_send(exc: BaseException, category: str) -> BaseException:
    """Tag an error raised before the message request was sent with its outbox category.

    Recipient refusals (``bad_request``) and missing credentials
    (``config_error``) would otherwise classify as ``unknown`` and be retried.
    """
    setattr(exc, "category", category)
    setattr(exc, "request_sent", False)
    return exc


def token_step_failed(channel: str, exc: ChannelHttpError) -> ChannelHttpError:
    """An access-token call failed, so the message itself was never sent.

    A timeout or 5xx on the token call must not mark the message ambiguous
    (nothing to reconcile); it is a plain retry. Rate limits, auth and config
    answers keep their category.
    """
    category = classify_channel_error(exc).category
    if category in {"timeout", "network_error", "provider_5xx", "unknown"}:
        category = "not_sent"
    return ChannelHttpError(
        f"{channel} access token request failed, so the message was not sent: {exc}",
        status_code=exc.status_code, channel=channel, request_sent=False, category=category,
    )
