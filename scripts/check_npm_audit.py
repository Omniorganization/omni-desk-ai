#!/usr/bin/env python3
"""Fail an npm audit report on any high or critical advisory without a governed exception.

An advisory is accepted only when release/security-exceptions/<GHSA>.md is a valid,
active, unexpired record (see scripts/check_security_exceptions.py), names the audited
npm project in its scope, and expires no more than MAX_EXCEPTION_DAYS from today.
Anything this script cannot read fails closed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_security_exceptions import (  # noqa: E402
    GHSA_RE,
    _parse_date,
    _parse_fields,
    _validate_exception,
)

BLOCKING_SEVERITIES = {"high", "critical"}
MAX_EXCEPTION_DAYS = 90


def blocking_advisories(report: object) -> tuple[dict[str, str], list[str]]:
    """Return ({GHSA: title} for high/critical advisories, problems that fail closed)."""
    if (
        not isinstance(report, dict)
        or "error" in report
        or report.get("auditReportVersion") != 2
        or not isinstance(report.get("vulnerabilities"), dict)
    ):
        return {}, ["npm audit did not produce a version 2 report"]
    found: dict[str, str] = {}
    problems: list[str] = []
    for name, vulnerability in report["vulnerabilities"].items():
        via = vulnerability.get("via") if isinstance(vulnerability, dict) else None
        if not isinstance(via, list):
            problems.append(f"{name}: malformed vulnerability entry")
            continue
        for advisory in via:
            if isinstance(advisory, str):
                continue  # inherited from another package; that package carries the advisory
            if not isinstance(advisory, dict):
                problems.append(f"{name}: malformed advisory entry")
                continue
            if advisory.get("severity") not in BLOCKING_SEVERITIES:
                continue
            ids = GHSA_RE.findall(str(advisory.get("url", "")))
            if len(ids) != 1:
                problems.append(f"{name}: blocking advisory without a single GHSA id: {advisory.get('url')}")
                continue
            found[ids[0]] = str(advisory.get("title") or name)
    counts = report.get("metadata", {}).get("vulnerabilities", {}) if isinstance(report.get("metadata"), dict) else {}
    if not found and not problems and any(counts.get(level, 0) for level in BLOCKING_SEVERITIES):
        problems.append("npm audit counts blocking vulnerabilities but names no advisory")
    return found, problems


def exception_issues(ghsa: str, project: str, exceptions_dir: Path, today: date) -> list[str]:
    path = exceptions_dir / f"{ghsa}.md"
    if not path.exists():
        return [f"{ghsa}: no governed exception record at {path}"]
    issues = _validate_exception(path, ghsa, today)
    fields = _parse_fields(path)
    if not re.search(rf"(?<![\w/.-]){re.escape(project)}(?![\w/.-])", fields.get("scope", "")):
        issues.append(f"{path}: scope does not name the audited project {project}")
    expires_at = _parse_date(fields.get("expires_at", ""))
    if expires_at is not None and expires_at > today + timedelta(days=MAX_EXCEPTION_DAYS):
        issues.append(f"{path}: expires_at may be at most {MAX_EXCEPTION_DAYS} days ahead")
    return issues


def check(report: object, project: str, exceptions_dir: Path, today: date) -> tuple[list[str], list[str]]:
    """Return (accepted GHSA ids, blocking issues)."""
    advisories, issues = blocking_advisories(report)
    accepted: list[str] = []
    for ghsa in sorted(advisories):
        problems = exception_issues(ghsa, project, exceptions_dir, today)
        if problems:
            issues.append(f"{ghsa} ({advisories[ghsa]}) is not covered by a governed exception")
            issues.extend(problems)
        else:
            accepted.append(ghsa)
    return accepted, issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", help="output of `npm audit --json`")
    parser.add_argument("--project", required=True, help="audited npm project path, e.g. apps/desktop-tauri")
    parser.add_argument("--exceptions", default="release/security-exceptions")
    args = parser.parse_args(argv)
    try:
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"BLOCKER cannot read npm audit report {args.report}: {exc}", file=sys.stderr)
        return 1
    today = datetime.now(timezone.utc).date()
    accepted, issues = check(report, args.project, Path(args.exceptions), today)
    for ghsa in accepted:
        print(f"ACCEPTED {ghsa} under {Path(args.exceptions) / (ghsa + '.md')}")
    if issues:
        for issue in issues:
            print(f"BLOCKER {issue}", file=sys.stderr)
        return 1
    print(f"npm audit policy passed: project={args.project} accepted_exceptions={len(accepted)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
