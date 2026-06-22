#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def _project_version(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise RuntimeError("pyproject.toml does not declare a project version")
    return match.group(1)


def _native_version(version: str) -> str:
    return version.split("+", 1)[0]


def _default_audit_path(root: Path) -> Path:
    return root / "release" / f"real-ga-evidence-audit-{_native_version(_project_version(root))}.json"


def _default_output_path(root: Path) -> Path:
    return root / "release" / f"real-ga-evidence-summary-{_native_version(_project_version(root))}.json"


def _git_head(root: Path) -> str:
    try:
        completed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=False)
    except OSError:
        return "unknown"
    return completed.stdout.strip() if completed.returncode == 0 and completed.stdout.strip() else "unknown"


def _github_context(env: Mapping[str, str]) -> dict[str, Any]:
    run_id = env.get("GITHUB_RUN_ID")
    if not run_id:
        return {"available": False}
    server_url = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    repository = env.get("GITHUB_REPOSITORY", "")
    run_url = f"{server_url}/{repository}/actions/runs/{run_id}" if repository else None
    return {
        "available": True,
        "repository": repository or None,
        "run_id": run_id,
        "run_attempt": env.get("GITHUB_RUN_ATTEMPT"),
        "workflow": env.get("GITHUB_WORKFLOW"),
        "job": env.get("GITHUB_JOB"),
        "ref": env.get("GITHUB_REF"),
        "sha": env.get("GITHUB_SHA"),
        "run_url": run_url,
    }


def build_summary(report: dict[str, Any], *, source_commit: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    categories: list[dict[str, Any]] = []
    blocking_categories: list[dict[str, Any]] = []
    for key, value in sorted((report.get("categories") or {}).items()):
        files = value.get("files") or []
        failed_files = [str(item.get("path")) for item in files if not bool(item.get("ok"))]
        issues = list(value.get("issues") or [])
        item = {
            "category": key,
            "label": value.get("label", key),
            "ok": bool(value.get("ok")),
            "required_file_count": len(files),
            "failed_file_count": len(failed_files),
            "failed_files": failed_files,
            "issue_count": len(issues),
            "issues": issues,
        }
        categories.append(item)
        if not item["ok"]:
            blocking_categories.append(
                {
                    "category": key,
                    "label": item["label"],
                    "failed_file_count": item["failed_file_count"],
                    "issue_count": item["issue_count"],
                }
            )
    blocker_count = int(report.get("blocker_count") or len(blocking_categories))
    status = str(report.get("status") or "unknown")
    return {
        "schema_version": "omnidesk-real-ga-evidence-summary/v1",
        "version": report.get("version"),
        "source_commit": source_commit,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "status": status,
        "real_ga_ready": status == "passed" and blocker_count == 0,
        "blocker_count": blocker_count,
        "blocking_categories": blocking_categories,
        "categories": categories,
        "github": _github_context(os.environ if env is None else env),
        "policy": report.get("policy", ""),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a compact machine-readable Real GA evidence summary from the external evidence audit.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--audit-report", help="External GA evidence audit JSON. Defaults to release/real-ga-evidence-audit-<native-version>.json.")
    parser.add_argument("--output", help="Summary output path. Defaults to release/real-ga-evidence-summary-<native-version>.json.")
    parser.add_argument("--source-commit", help="Source commit to bind into the summary. Defaults to GITHUB_SHA or git HEAD.")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    audit_path = Path(args.audit_report) if args.audit_report else _default_audit_path(root)
    if not audit_path.is_absolute():
        audit_path = root / audit_path
    output = Path(args.output) if args.output else _default_output_path(root)
    if not output.is_absolute():
        output = root / output
    source_commit = args.source_commit or os.environ.get("GITHUB_SHA") or _git_head(root)

    report = json.loads(audit_path.read_text(encoding="utf-8"))
    summary = build_summary(report, source_commit=source_commit)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(output), "status": summary["status"], "blocker_count": summary["blocker_count"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
