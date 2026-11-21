"""Task-success scenarios for the agent loop.

A scenario is a task, the workspace files it starts with, which approvals a
careful human would grant, the checks that decide success, and what must never
happen. `script` holds the model turns for the deterministic CI mode; the live
mode ignores it and asks a real model.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUCCESS_CHECKS = frozenset({
    "status_in",
    "final_answer_contains",
    "final_answer_not_contains",
    "file_equals",
    "file_contains",
    "file_not_contains",
    "file_absent",
    "max_steps",
})
FORBIDDEN_CHECKS = frozenset({"file_exists", "tool_action"})


@dataclass(frozen=True)
class AgentScenario:
    id: str
    task: str
    success: tuple[dict[str, Any], ...]
    title: str = ""
    channel: str = "local-cli"
    sender: str = "eval-user"
    files: dict[str, str] = field(default_factory=dict)
    allow_write: bool = True
    approve: tuple[dict[str, Any], ...] = ()
    forbidden: tuple[dict[str, Any], ...] = ()
    budget: dict[str, Any] = field(default_factory=dict)
    script: tuple[Any, ...] = ()
    tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source: str = "") -> "AgentScenario":
        where = f"{source}: " if source else ""
        for key in ("id", "task", "success"):
            if not data.get(key):
                raise ValueError(f"{where}scenario needs '{key}'")
        unknown = set(data) - {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        if unknown:
            raise ValueError(f"{where}unknown scenario keys: {sorted(unknown)}")
        for check in data["success"]:
            if check.get("type") not in SUCCESS_CHECKS:
                raise ValueError(f"{where}unknown success check: {check.get('type')!r}")
        for check in data.get("forbidden") or ():
            if check.get("type") not in FORBIDDEN_CHECKS:
                raise ValueError(f"{where}unknown forbidden check: {check.get('type')!r}")
        return cls(
            id=str(data["id"]),
            task=str(data["task"]),
            success=tuple(data["success"]),
            title=str(data.get("title") or ""),
            channel=str(data.get("channel") or "local-cli"),
            sender=str(data.get("sender") or "eval-user"),
            files={str(k): str(v) for k, v in (data.get("files") or {}).items()},
            allow_write=bool(data.get("allow_write", True)),
            approve=tuple(data.get("approve") or ()),
            forbidden=tuple(data.get("forbidden") or ()),
            budget=dict(data.get("budget") or {}),
            script=tuple(data.get("script") or ()),
            tags=tuple(str(t) for t in data.get("tags") or ()),
        )


def load_agent_scenarios(path: Path) -> list[AgentScenario]:
    """Scenarios from one JSON file, or from every *.json file of a directory (sorted)."""
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    scenarios: list[AgentScenario] = []
    for file in files:
        raw = json.loads(file.read_text(encoding="utf-8"))
        items = raw.get("scenarios", [raw]) if isinstance(raw, dict) else raw
        scenarios.extend(AgentScenario.from_dict(item, source=file.name) for item in items)
    ids = [s.id for s in scenarios]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"duplicate scenario ids: {duplicates}")
    return scenarios


def default_scenarios_dir() -> Path:
    return Path(__file__).with_name("scenarios")
