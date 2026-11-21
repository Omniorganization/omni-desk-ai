"""Reading the security and admin audit logs, for Web Admin's audit view.

``GET /admin/audit/events`` is for auditors and owners only. A viewer or an
operator cannot use it, and neither can a break-glass elevation.

- ``log``: ``security`` (the permission gate's log) or ``admin`` (admin
  authentication and admin sessions).
- ``after_seq`` and ``limit`` (1 to 500) page through the chain in order.
  ``next_after_seq`` is set while more entries may follow.
- Organization-scoped: an entry belongs to the organization of its actor.
  Entries with no actor, such as chain rotations, are deployment-level and are
  shown only to callers in ``org_default``.
- Redacted: argument values are withheld (their digests and lengths are kept),
  content fields such as message text and tool output are withheld,
  secret-named keys are masked, and credentials inside the remaining strings
  are masked.
- ``verification`` reports the background chain check for that log: ``ok``,
  ``failed`` or ``not_checked``, when it last ran, the last sequence number it
  verified, and its error. Entries after ``last_verified_seq`` are newer than
  the last check.

The log is read on a worker thread, a line at a time.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request

from omnidesk_agent.privacy.redaction import MemoryPrivacyFilter
from omnidesk_agent.security.audit_chain import CHAIN_FORMAT, GENESIS_EVENT
from omnidesk_agent.server_routes.agent_routes import AdminVerifier, _authenticated_actor, _reject_break_glass_elevation

MAX_LIMIT = 500
_ACTOR_KEYS = ("actor", "requested_by", "created_by", "decided_by")
_CONTENT_KEYS = frozenset({"text", "content", "body", "message", "prompt", "output", "result", "stdout", "stderr", "response", "payload_text"})
_SECRET_KEY_RE = re.compile(r"(?i)(token|secret|password|passwd|api[_-]?key|authorization|cookie|private[_-]?key|credential)")
_MAX_STRING = 500
_MAX_ITEMS = 50
_SECRETS = MemoryPrivacyFilter()


def _organization(rt: Any, actor: str) -> str:
    resolve = getattr(getattr(rt, "app_sync", None), "_organization_for_actor", None)
    return str(resolve(actor)) if callable(resolve) and actor else "org_default"


def _redact(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return _SECRETS.redact_secrets(value)[:_MAX_STRING]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if depth >= 3:
        return "[WITHHELD]"
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in list(value)[:_MAX_ITEMS]]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:_MAX_ITEMS]:
            key = str(key)
            if key == "args" and isinstance(item, dict):
                kept = {k: v for k, v in item.items() if str(k).endswith(("_sha256", "_length")) and isinstance(v, (str, int))}
                out["args"] = kept
                out["args_withheld"] = len(kept) != len(item)
            elif key.lower() in _CONTENT_KEYS:
                out[key] = "[WITHHELD]"
            elif _SECRET_KEY_RE.search(key) and not key.endswith(("_sha256", "_length", "_env")):
                out[key] = "[REDACTED]"
            else:
                out[key] = _redact(item, depth + 1)
        return out
    return "[WITHHELD]"


def _entry_actor(payload: dict[str, Any]) -> str:
    for key in _ACTOR_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _read_page(rt: Any, path: Path, *, organization: str, after_seq: int, limit: int) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    organizations: dict[str, str] = {}
    unreadable = 0
    more = False
    if not path.exists():
        return {"events": events, "next_after_seq": None, "unreadable_lines": 0}
    with path.open("rb") as handle:
        for raw in handle:
            try:
                record = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                unreadable += 1
                continue
            # Pre-chain lines have no sequence number to page by; the verifier covers them.
            if not isinstance(record, dict) or record.get("chain") != CHAIN_FORMAT or record.get("event") == GENESIS_EVENT:
                continue
            seq = record.get("seq")
            if not isinstance(seq, int) or seq <= after_seq:
                continue
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            actor = _entry_actor(payload)
            if actor not in organizations:
                organizations[actor] = _organization(rt, actor) if actor else "org_default"
            if organizations[actor] != organization:
                continue
            if len(events) == limit:
                more = True
                break
            events.append({"seq": seq, "ts": record.get("ts"), "event": record.get("event"), "actor": actor or None, "payload": _redact(payload)})
    return {"events": events, "next_after_seq": events[-1]["seq"] if more and events else None, "unreadable_lines": unreadable}


def _verification(app: FastAPI, rt: Any, path: Path) -> dict[str, Any]:
    monitor = getattr(app.state, "audit_monitor", None)
    failure = getattr(monitor, "failure", None) or getattr(getattr(rt, "permissions", None), "audit_integrity_failure", None)
    report = next((r for r in getattr(monitor, "last_reports", None) or [] if r.get("path") == str(path)), None)
    if failure or (report is not None and report.get("ok") is False):
        status = "failed"
    elif report is not None:
        status = "ok"
    else:
        status = "not_checked"
    error = (report or {}).get("error") or failure
    return {
        "status": status,
        "checked_at": getattr(monitor, "last_checked_at", None),
        "last_verified_seq": (report or {}).get("last_seq"),
        "error": str(error)[:300] if error else None,
    }


def register_audit_routes(app: FastAPI, cfg: Any, rt: Any, admin: AdminVerifier) -> None:
    @app.get("/admin/audit/events")
    async def audit_events(
        request: Request,
        log: Literal["security", "admin"] = "security",
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=MAX_LIMIT),
    ):
        decision = await admin(request, "auditor")
        _reject_break_glass_elevation(decision, "read the audit logs")
        actor = _authenticated_actor(decision)
        organization = getattr(decision, "organization", None) or await asyncio.to_thread(_organization, rt, actor)
        path: Optional[Path] = cfg.permissions.audit_log if log == "security" else getattr(getattr(rt, "admin_auth", None), "audit_log", None)
        if path is None:
            raise HTTPException(404, f"this gateway keeps no {log} audit log")
        page = await asyncio.to_thread(_read_page, rt, Path(path).expanduser(), organization=organization, after_seq=after_seq, limit=limit)
        return {"ok": True, "log": log, "organization": organization, **page, "verification": _verification(request.app, rt, Path(path).expanduser())}
