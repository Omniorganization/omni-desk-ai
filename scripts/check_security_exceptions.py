#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REQUIRED_FIELDS = {
    "exception_id",
    "status",
    "owner",
    "scope",
    "expires_at",
    "upstream_tracking",
    "compensating_controls",
}
PLACEHOLDER_VALUES = {"", "todo", "tbd", "n/a", "none", "unknown", "change-me", "replace-me"}
ALLOW_GHSA_RE = re.compile(r"allow-ghsas:\s*([^\n#]+)")
GHSA_RE = re.compile(r"GHSA-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}")
FIELD_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 _-]*):\s*(.*?)\s*$")


def _root_from_exception_dir(exception_dir: Path) -> Path:
    # release/security-exceptions -> repository root
    if exception_dir.name == "security-exceptions" and exception_dir.parent.name == "release":
        return exception_dir.parent.parent
    return Path.cwd()


def _allowed_ghsas_from_workflows(root: Path) -> set[str]:
    workflows = root / ".github" / "workflows"
    if not workflows.exists():
        return set()
    allowed: set[str] = set()
    for path in workflows.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        for match in ALLOW_GHSA_RE.finditer(text):
            allowed.update(GHSA_RE.findall(match.group(1)))
    for path in workflows.glob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        for match in ALLOW_GHSA_RE.finditer(text):
            allowed.update(GHSA_RE.findall(match.group(1)))
    return allowed


def _parse_fields(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        match = FIELD_RE.match(raw_line.strip())
        if not match:
            continue
        key = match.group(1).strip().lower().replace(" ", "_").replace("-", "_")
        fields[key] = match.group(2).strip()
    return fields


def _parse_date(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def _validate_exception(path: Path, expected_id: str | None, today: date) -> list[str]:
    issues: list[str] = []
    fields = _parse_fields(path)
    missing = sorted(REQUIRED_FIELDS - set(fields))
    if missing:
        issues.append(f"{path}: missing required fields: {missing}")
        return issues

    for field in sorted(REQUIRED_FIELDS):
        value = fields.get(field, "").strip()
        if value.lower() in PLACEHOLDER_VALUES:
            issues.append(f"{path}: {field} uses a placeholder value")

    exception_id = fields.get("exception_id", "")
    if expected_id and exception_id != expected_id:
        issues.append(f"{path}: exception_id must be {expected_id}")
    if not GHSA_RE.fullmatch(exception_id):
        issues.append(f"{path}: exception_id must be a GHSA id")

    status = fields.get("status", "").strip().lower()
    if status not in {"active", "mitigated", "expired"}:
        issues.append(f"{path}: status must be active, mitigated, or expired")
    if status != "active":
        issues.append(f"{path}: dependency-review allowlist exceptions must be active until removed")

    expires_at = _parse_date(fields.get("expires_at", ""))
    if expires_at is None:
        issues.append(f"{path}: expires_at must be YYYY-MM-DD")
    elif expires_at < today:
        issues.append(f"{path}: exception expired on {expires_at.isoformat()}")

    text = path.read_text(encoding="utf-8").lower()
    for required_phrase in ("impact", "runtime reachability", "compensating control", "removal criteria"):
        if required_phrase not in text:
            issues.append(f"{path}: body must document {required_phrase}")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate governed security exception records.")
    parser.add_argument("exception_dir", nargs="?", default="release/security-exceptions")
    args = parser.parse_args(argv)

    exception_dir = Path(args.exception_dir).resolve()
    root = _root_from_exception_dir(exception_dir)
    today = datetime.now(timezone.utc).date()
    issues: list[str] = []

    allowed_ghsas = _allowed_ghsas_from_workflows(root)
    if allowed_ghsas and not exception_dir.exists():
        issues.append(f"security exception directory is required: {exception_dir}")
    if exception_dir.exists():
        for path in sorted(exception_dir.glob("GHSA-*.md")):
            expected = path.stem
            issues.extend(_validate_exception(path, expected, today))

    for ghsa in sorted(allowed_ghsas):
        path = exception_dir / f"{ghsa}.md"
        if not path.exists():
            issues.append(f"allowed advisory {ghsa} requires governed exception file: {path}")

    if issues:
        for issue in issues:
            print(f"BLOCKER {issue}", file=sys.stderr)
        return 1
    print(f"security exception policy passed: allowed_ghsa_count={len(allowed_ghsas)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
