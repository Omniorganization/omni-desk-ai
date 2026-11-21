"""Verify a proposed diff in an isolated worktree and produce the evidence bundle.

A proposal passes only if: the diff passes ``DiffPolicy``; named failing tests
fail before the change (the issue is reproduced); the diff applies cleanly and
changes exactly the files it declares; at least one test covers the change and
the targeted tests pass; ruff, bandit (medium+), compileall and
``git diff --check`` pass on the changed files; and the full suite passes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from omnidesk_agent.self_upgrade.pipeline.diff_policy import DiffPolicy
from omnidesk_agent.self_upgrade.pipeline.issue import UpgradeIssue
from omnidesk_agent.self_upgrade.pipeline.workspace import IsolatedWorktree

SCHEMA = "omnidesk-self-upgrade-evidence/v1"


@dataclass
class EvidenceBundle:
    schema: str
    ok: bool
    reasons: list[str]
    issue: dict[str, Any]
    base_sha: str
    patch_sha256: str
    diff_report: dict[str, Any]
    changed_files: list[str] = field(default_factory=list)
    targeted_tests: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    full_suite_passed: bool = False
    produced_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def patch_sha256(diff: str) -> str:
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


def module_name(path: str) -> Optional[str]:
    if not path.endswith(".py") or not path.startswith("omnidesk_agent/"):
        return None
    module = path[:-3].replace("/", ".")
    return module[: -len(".__init__")] if module.endswith(".__init__") else module


def select_targeted_tests(root: Path, changed: list[str], issue: UpgradeIssue) -> list[str]:
    targets = set(issue.failing_tests)
    targets.update(p for p in changed if p.startswith("tests/") and Path(p).name.startswith("test_") and p.endswith(".py"))
    modules = [m for m in (module_name(p) for p in changed) if m]
    if modules:
        pattern = re.compile(r"^\s*(?:from|import)\s+(" + "|".join(re.escape(m) for m in modules) + r")\b", re.MULTILINE)
        for test in sorted((Path(root) / "tests").glob("test_*.py")):
            if pattern.search(test.read_text(encoding="utf-8", errors="replace")):
                targets.add(str(test.relative_to(root)))
    return sorted(targets)


def verify(
    repo_root: Path,
    diff: str,
    issue: UpgradeIssue,
    *,
    base_ref: str = "HEAD",
    python: str = sys.executable,
    run_full_suite: bool = True,
    policy: Optional[DiffPolicy] = None,
) -> EvidenceBundle:
    policy = policy or DiffPolicy(allowed_prefixes=tuple(issue.scope_paths) + (("tests/",) if issue.scope_paths else ()))
    report = policy.check(diff)
    bundle = EvidenceBundle(SCHEMA, False, [], issue.to_dict(), "", patch_sha256(diff), report.to_dict())
    if not report.ok:
        bundle.reasons = ["diff policy: " + v for v in report.violations]
        return _stamp(bundle)
    pytest = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    with IsolatedWorktree(repo_root, base_ref) as tree:
        bundle.base_sha = tree.base_sha
        if issue.failing_tests:
            before = tree.run(pytest + list(issue.failing_tests))
            bundle.steps.append({"name": "failing_tests_before_change", **before.to_dict()})
            if before.exit_code == 0:
                bundle.reasons.append("the named failing tests pass before the change; the issue is not reproduced")
        applied = tree.apply(diff)
        bundle.steps.append({"name": "apply", **applied.to_dict()})
        if applied.exit_code != 0:
            bundle.reasons.append("diff does not apply to the base commit")
            return _stamp(bundle)
        bundle.changed_files = tree.changed_files()
        if sorted(bundle.changed_files) != sorted(report.files):
            # Never run code from a patch that touched files the policy did not check.
            bundle.reasons.append(f"applied files {bundle.changed_files} differ from the diff headers {report.files}")
            return _stamp(bundle)
        bundle.targeted_tests = select_targeted_tests(tree.path, bundle.changed_files, issue)  # type: ignore[arg-type]
        if not bundle.targeted_tests:
            bundle.reasons.append("no test covers the change; the diff must add or touch a test")
        else:
            targeted = tree.run(pytest + ["-W", "error"] + bundle.targeted_tests)
            bundle.steps.append({"name": "targeted_tests", **targeted.to_dict()})
            if targeted.exit_code != 0:
                bundle.reasons.append("targeted tests fail")
        py_files = [p for p in bundle.changed_files if p.endswith(".py")]
        gates = [("git_diff_check", ["git", "diff", "--check"])]
        if py_files:
            gates += [
                ("ruff", [python, "-m", "ruff", "check", *py_files]),
                ("bandit", [python, "-m", "bandit", "-q", "-ll", *py_files]),
                ("compileall", [python, "-m", "compileall", "-q", *py_files]),
            ]
        for name, argv in gates:
            result = tree.run(argv)
            bundle.steps.append({"name": name, **result.to_dict()})
            if result.exit_code != 0:
                bundle.reasons.append(f"gate failed: {name}")
        if run_full_suite and not bundle.reasons:
            full = tree.run(pytest + ["-W", "error", "tests"], timeout=3600)
            bundle.steps.append({"name": "full_suite", **full.to_dict()})
            if full.exit_code != 0:
                bundle.reasons.append("full test suite fails")
    bundle.ok = not bundle.reasons
    bundle.full_suite_passed = any(step["name"] == "full_suite" and step["exit_code"] == 0 for step in bundle.steps)
    return _stamp(bundle)


def _stamp(bundle: EvidenceBundle) -> EvidenceBundle:
    bundle.produced_at = dt.datetime.now(dt.timezone.utc).isoformat()
    return bundle
