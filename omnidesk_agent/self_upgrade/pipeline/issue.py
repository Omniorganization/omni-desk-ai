"""A scoped issue the self-upgrade pipeline may try to fix."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

_NODEID = re.compile(r"^tests/[A-Za-z0-9_./-]+\.py(::[A-Za-z0-9_\[\]-]+)*$")
_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")


@dataclass(frozen=True)
class UpgradeIssue:
    title: str
    body: str
    source: str  # github_issue | repair_request | manual
    reference: str = ""
    scope_paths: tuple[str, ...] = ()
    failing_tests: tuple[str, ...] = ()
    labels: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("issue title is required")
        if len(self.body) > 20_000:
            raise ValueError("issue body is too long")
        for path in self.scope_paths:
            if not _PATH.match(path) or ".." in path.split("/"):
                raise ValueError(f"invalid scope path: {path}")
        for nodeid in self.failing_tests:
            if not _NODEID.match(nodeid):
                raise ValueError(f"invalid test node id: {nodeid}")

    def slug(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")
        return slug[:40].strip("-") or "fix"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UpgradeIssue":
        return cls(
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            source=str(data.get("source") or "manual"),
            reference=str(data.get("reference") or ""),
            scope_paths=tuple(data.get("scope_paths") or ()),
            failing_tests=tuple(data.get("failing_tests") or ()),
            labels=tuple(data.get("labels") or ()),
        )

    @classmethod
    def from_github_issue(cls, payload: dict[str, Any], *, required_label: str = "self-upgrade") -> "UpgradeIssue":
        """Only issues a maintainer labelled for the pipeline are accepted.

        Scope and failing tests come from fenced ``scope:`` / ``tests:`` lines in
        the body, e.g. ``scope: omnidesk_agent/tools/web/``; ``scope:`` is required
        (``diff_policy.scoped_policy``).
        """
        labels = tuple(str(label.get("name") if isinstance(label, dict) else label) for label in payload.get("labels") or [])
        if required_label not in labels:
            raise PermissionError(f"issue is not labelled {required_label!r}")
        body = str(payload.get("body") or "")
        scope = tuple(m.strip() for line in body.splitlines() if line.lower().startswith("scope:") for m in line[6:].split(",") if m.strip())
        tests = tuple(m.strip() for line in body.splitlines() if line.lower().startswith("tests:") for m in line[6:].split(",") if m.strip())
        return cls(
            title=str(payload.get("title") or ""),
            body=body,
            source="github_issue",
            reference=f"#{payload.get('number')}",
            scope_paths=scope,
            failing_tests=tests,
            labels=labels,
        )

    @classmethod
    def from_repair_request(cls, path: Path) -> "UpgradeIssue":
        """A self-healing repair request (``<workspace>/repair_requests/*.json``)."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        component = str(data.get("component") or "unknown")
        return cls(
            title=f"Repair repeated failures in {component}",
            body=f"Self-healing could not contain repeated failures.\nReason: {data.get('reason')}\nSignal: {json.dumps(data.get('signal'), ensure_ascii=False)}",
            source="repair_request",
            reference=str(data.get("fingerprint") or ""),
        )


def load_issue(path: Path, *, kind: Optional[str] = None) -> UpgradeIssue:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if kind == "github_issue" or ("number" in data and "labels" in data and "source" not in data):
        return UpgradeIssue.from_github_issue(data)
    return UpgradeIssue.from_dict(data)
