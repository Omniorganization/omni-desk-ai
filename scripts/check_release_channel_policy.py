#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


AUDIT_PATH_RE = re.compile(r"release/real-ga-evidence-audit-(\d+\.\d+\.\d+)\.json")
CANDIDATE_MARKERS = ("candidate", "source-gated")


def _read(path: Path) -> str:
    if not path.exists():
        raise RuntimeError(f"missing required file: {path}")
    return path.read_text(encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(_read(path))


def _project_version(root: Path) -> str:
    match = re.search(r'^version\s*=\s*"([^"]+)"', _read(root / "pyproject.toml"), re.MULTILINE)
    if not match:
        raise RuntimeError("pyproject.toml does not declare a project version")
    return match.group(1)


def _native_version(version: str) -> str:
    return version.split("+", 1)[0]


def _makefile_value(makefile: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\s*\?=\s*(.+)$", makefile, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _extract_real_ga_branch(workflow: str) -> str:
    marker = 'if [[ "$RELEASE_CHANNEL" == "real-ga" ]]; then'
    start = workflow.rfind(marker)
    if start < 0:
        return ""
    match = re.search(
        r'if \[\[ "\$RELEASE_CHANNEL" == "real-ga" \]\]; then(?P<body>.*?)elif \[\[ "\$RELEASE_CHANNEL" == "candidate" \]\]; then',
        workflow[start:],
        re.DOTALL,
    )
    return match.group("body") if match else ""


def _extract_candidate_branch(workflow: str) -> str:
    marker = 'elif [[ "$RELEASE_CHANNEL" == "candidate" ]]; then'
    start = workflow.rfind(marker)
    if start < 0:
        return ""
    match = re.search(
        r'elif \[\[ "\$RELEASE_CHANNEL" == "candidate" \]\]; then(?P<body>.*?)else\s+echo "release_channel must be candidate or real-ga"',
        workflow[start:],
        re.DOTALL,
    )
    return match.group("body") if match else ""


def _generated_package_entries(root: Path) -> list[str]:
    blocked: list[str] = []
    for path in root.iterdir():
        name = path.name
        if path.is_dir() and (name.startswith("Omni-desk-AI-") or name.startswith("OmniDesk-AI-")):
            blocked.append(name)
        elif path.is_file() and name.startswith("Omni-desk-AI-") and name.endswith(".zip"):
            blocked.append(name)
    return sorted(blocked)


def _check(condition: bool, message: str, failures: list[str], ok: list[str]) -> None:
    (ok if condition else failures).append(message)


def _has_candidate_marker(value: str) -> bool:
    lower = value.lower()
    return any(marker in lower for marker in CANDIDATE_MARKERS)


def _check_channel_naming(
    *,
    release_channel: str,
    package_version: str,
    package_slug: str,
    failures: list[str],
    ok: list[str],
) -> None:
    if release_channel == "candidate":
        _check(
            _has_candidate_marker(package_version),
            "Candidate package version carries candidate/source-gated status",
            failures,
            ok,
        )
        _check(
            _has_candidate_marker(package_slug),
            "Candidate package slug carries candidate/source-gated status",
            failures,
            ok,
        )
    elif release_channel == "real-ga":
        _check(
            not _has_candidate_marker(package_version),
            "Real GA package version does not carry candidate/source-gated status",
            failures,
            ok,
        )
        _check(
            not _has_candidate_marker(package_slug),
            "Real GA package slug does not carry candidate/source-gated status",
            failures,
            ok,
        )
    else:
        failures.append("release channel must be candidate or real-ga")


def check_policy(
    root: Path,
    *,
    release_channel: str = "candidate",
    package_version_override: str | None = None,
    package_slug_override: str | None = None,
) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    ok: list[str] = []

    version = _project_version(root)
    native = _native_version(version)
    workflow = _read(root / ".github/workflows/release.yml")
    makefile = _read(root / "Makefile")
    external_gate = _read(root / "scripts/check_external_ga_evidence.py")
    distribution_manifest = _read(root / "scripts/write_distribution_manifest.py")
    branch_protection_doc = _read(root / "docs/BRANCH_PROTECTION.md")
    codeowners = _read(root / ".github/CODEOWNERS")
    gitignore = _read(root / ".gitignore")

    audit_paths = sorted(set(AUDIT_PATH_RE.findall(workflow + "\n" + makefile)))
    active_audit_rel = f"release/real-ga-evidence-audit-{native}.json"
    active_audit = root / active_audit_rel
    real_ga_branch = _extract_real_ga_branch(workflow)
    candidate_branch = _extract_candidate_branch(workflow)
    package_version = package_version_override or _makefile_value(makefile, "DISTRIBUTION_PACKAGE_VERSION")
    package_slug = package_slug_override or _makefile_value(makefile, "DISTRIBUTION_PACKAGE_SLUG")

    _check("release_channel" in workflow and "real-ga" in workflow and "candidate" in workflow, "Release workflow exposes candidate and real-ga channels", failures, ok)
    _check(bool(real_ga_branch), "Release workflow has an explicit real-ga branch", failures, ok)
    _check(bool(candidate_branch), "Release workflow has an explicit candidate branch", failures, ok)
    _check("--audit-only" not in real_ga_branch, "Real GA branch never uses --audit-only", failures, ok)
    _check("--audit-only" in candidate_branch, "Candidate branch uses audit-only external evidence reporting", failures, ok)
    _check("--release-channel \"$RELEASE_CHANNEL\"" in workflow and "--package-version \"$EXPECTED_VERSION\"" in workflow, "Release workflow rechecks channel naming and evidence status after external evidence gate", failures, ok)
    _check(active_audit_rel in workflow and active_audit_rel in makefile, "Active release audit report is wired through workflow and Makefile", failures, ok)
    _check(native in audit_paths, "Active release audit path matches the project native version", failures, ok)
    _check(active_audit.exists(), "Active release audit report exists", failures, ok)
    _check_channel_naming(
        release_channel=release_channel,
        package_version=package_version,
        package_slug=package_slug,
        failures=failures,
        ok=ok,
    )
    _check(
        'return 0 if args.audit_only or report["status"] == "passed" else 1' in external_gate,
        "External GA evidence gate fails closed unless report status is passed",
        failures,
        ok,
    )
    _check(
        '"customer_distribution_ga" if blocker_count == 0 else "source_gated_production_ga_candidate"' in distribution_manifest,
        "Distribution manifest only promotes to customer_distribution_ga when blocker_count is zero",
        failures,
        ok,
    )

    if active_audit.exists():
        report = _read_json(active_audit)
        _check(report.get("version") == version, "Active release audit version matches pyproject", failures, ok)
        blocker_count = report.get("blocker_count")
        status = report.get("status")
        _check(blocker_count == 0 or status != "passed", "Blocked audit reports cannot claim passed status", failures, ok)
        _check(blocker_count != 0 or status == "passed", "Passed audit reports must have blocker_count zero", failures, ok)
        if release_channel == "real-ga":
            _check(blocker_count == 0, "Real GA evidence blocker_count is zero", failures, ok)
            _check(status == "passed", "Real GA evidence status is passed", failures, ok)

    for owned in (".github/workflows/", "scripts/", "deploy/", "omnidesk_agent/security/", "release/"):
        _check(owned in codeowners, f"CODEOWNERS covers {owned}", failures, ok)

    protection = root / ".github/branch-protection.required.json"
    _check(protection.exists(), "Source-controlled branch protection contract exists", failures, ok)
    _check(
        (root / "scripts/check_github_branch_protection_live.py").exists()
        and "check_github_branch_protection_live.py" in branch_protection_doc,
        "Live GitHub branch protection verifier is documented",
        failures,
        ok,
    )
    if protection.exists():
        policy = _read_json(protection)
        required_checks = set(policy.get("required_status_checks", []))
        for required in ("CI", "Security", "release-policy", "external-ga-evidence-contract"):
            _check(required in required_checks, f"Branch protection contract requires {required}", failures, ok)
        _check(policy.get("allow_direct_pushes") is False, "Branch protection contract forbids direct pushes", failures, ok)

    _check("Omni-desk-AI-*/" in gitignore, "Generated OmniDesk package directories are ignored at source root", failures, ok)
    _check("Omni-desk-AI-*.zip" in gitignore, "Generated OmniDesk wrapper zips are ignored at source root", failures, ok)
    _check(not _generated_package_entries(root), "Source root contains no generated OmniDesk package directories or wrapper zips", failures, ok)

    return failures, ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check OmniDesk release channel, branch-protection, and source-trunk package policy.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--release-channel", choices=("candidate", "real-ga"), default="candidate")
    parser.add_argument("--package-version")
    parser.add_argument("--package-slug")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        failures, ok = check_policy(
            root,
            release_channel=args.release_channel,
            package_version_override=args.package_version,
            package_slug_override=args.package_slug,
        )
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for message in ok:
        print(f"OK      {message}")
    for message in failures:
        print(f"BLOCKER {message}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
