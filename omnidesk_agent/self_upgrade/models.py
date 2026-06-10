from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

RiskLevel = Literal["low", "medium", "high", "critical"]
UpgradeStatus = Literal[
    "draft",
    "approved",
    "patched",
    "tested",
    "failed",
    "committed",
    "pushed",
    "pr_opened",
]


@dataclass
class UpgradeRequest:
    title: str
    reason: str
    source: str = "local-cli"
    related_logs: list[str] = field(default_factory=list)
    risk: RiskLevel = "medium"


@dataclass
class UpgradePlan:
    title: str
    goal: str
    files_to_change: list[str]
    test_commands: list[str]
    rollback_plan: str
    risk: RiskLevel
    requires_human_approval: bool = True
    notes: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        files = "\n".join(f"- `{f}`" for f in self.files_to_change) or "- None"
        tests = "\n".join(f"- `{c}`" for c in self.test_commands) or "- Manual review only"
        notes = "\n".join(f"- {n}" for n in self.notes) or "- No extra notes"
        return f"""# Upgrade Plan: {self.title}

## Goal
{self.goal}

## Risk
{self.risk}

## Requires Human Approval
{self.requires_human_approval}

## Files to Change
{files}

## Test Commands
{tests}

## Rollback Plan
{self.rollback_plan}

## Notes
{notes}
"""


@dataclass
class PatchResult:
    changed_files: list[str]
    summary: str
    diff: str


@dataclass
class TestResult:
    ok: bool
    command: str
    output: str
    exit_code: int


@dataclass
class UpgradeRun:
    request: UpgradeRequest
    plan: UpgradePlan
    patch: PatchResult | None = None
    tests: list[TestResult] = field(default_factory=list)
    status: UpgradeStatus = "draft"
    branch: str | None = None
    pull_request_url: str | None = None
