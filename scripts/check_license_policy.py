#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from importlib import metadata
from pathlib import Path
from typing import Any


LOCK_PACKAGE_RE = re.compile(r"^([A-Za-z0-9_.-]+)==[^\s;]+(?:\s*;\s*(.+?))?\s*\\?$")


def _normalise_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _load_policy(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != "omnidesk-license-policy/v1":
        raise RuntimeError("license policy schema_version must be omnidesk-license-policy/v1")
    return data


def _marker_applies(marker: str | None) -> bool:
    if not marker:
        return True
    try:
        from packaging.markers import Marker

        return bool(Marker(marker).evaluate())
    except Exception:
        return True


def _packages_from_lockfile(path: Path) -> list[str]:
    packages: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = LOCK_PACKAGE_RE.match(raw.strip())
        if match and _marker_applies(match.group(2)):
            packages.append(match.group(1))
    return sorted(set(packages), key=_normalise_name)


def _distribution_text(package: str) -> str:
    dist = metadata.distribution(package)
    fields: list[str] = []
    for key in ("Name", "License-Expression", "License", "Summary"):
        value = dist.metadata.get(key)
        if value:
            fields.append(str(value))
    fields.extend(dist.metadata.get_all("Classifier") or [])
    return "\n".join(fields)


def _matches_denied_license(text: str, denied_terms: list[str]) -> list[str]:
    hits: list[str] = []
    for term in denied_terms:
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", re.IGNORECASE)
        if pattern.search(text):
            hits.append(term)
    return hits


def check(lockfile: Path, policy_path: Path) -> tuple[list[str], list[str]]:
    policy = _load_policy(policy_path)
    denied_terms = [str(item) for item in policy.get("denied_license_terms", [])]
    allowed_packages = {_normalise_name(str(item)) for item in policy.get("allowed_packages", [])}
    allow_unknown = bool(policy.get("allow_unknown_license", False))
    failures: list[str] = []
    warnings: list[str] = []
    for package in _packages_from_lockfile(lockfile):
        normalised = _normalise_name(package)
        if normalised in allowed_packages:
            continue
        try:
            license_text = _distribution_text(package)
        except metadata.PackageNotFoundError:
            failures.append(f"{package}: package from lockfile is not installed for license inspection")
            continue
        if not license_text.strip():
            (warnings if allow_unknown else failures).append(f"{package}: license metadata is empty")
            continue
        hits = _matches_denied_license(license_text, denied_terms)
        if hits:
            failures.append(f"{package}: denied license term(s): {', '.join(hits)}")
    return failures, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check installed dependencies from a hashed lockfile against the OmniDesk license policy.")
    parser.add_argument("--lockfile", required=True)
    parser.add_argument("--policy", required=True)
    args = parser.parse_args(argv)
    try:
        failures, warnings = check(Path(args.lockfile), Path(args.policy))
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for warning in warnings:
        print(f"WARN    {warning}")
    if failures:
        for failure in failures:
            print(f"BLOCKER {failure}", file=sys.stderr)
        return 1
    print("license policy gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
