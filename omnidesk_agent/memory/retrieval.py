"""Authorization and review eligibility for task-planner memory context."""
from __future__ import annotations

import math
from typing import Any


def known_memory_identity(channel: str | None, actor: str | None) -> bool:
    return all(isinstance(value, str) and value.strip() and value != "unknown" for value in (channel, actor))


def planner_memory_visible(row: dict[str, Any], *, channel: str, actor: str, now: float) -> bool:
    # Never infer ownership from tags, namespace strings or the requesting actor.
    if not known_memory_identity(channel, actor) or row.get("channel") != channel or row.get("actor") != actor:
        return False
    if row.get("memory_status") not in {"candidate", "validated", "trusted"}:
        return False
    expiry = row.get("expires_at")
    if expiry is None:
        return True
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        return False
    return math.isfinite(expiry) and expiry > now


def matches_memory_query(row: dict[str, Any], query: str) -> bool:
    # Match literal tokens, including punctuation, without executing FTS syntax.
    terms = query.casefold().split()
    text = " ".join(str(row.get(field) or "") for field in (
        "goal", "failure_reason", "recommended_next_action", "tags",
    )).casefold()
    return bool(terms) and all(term in text for term in terms)
