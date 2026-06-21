#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _project_version(root: Path) -> str:
    text = _read_text(root / "pyproject.toml")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise RuntimeError("pyproject.toml does not declare a project version")
    return match.group(1)


def _git_commit(root: Path) -> str:
    env_sha = os.getenv("GITHUB_SHA", "").strip()
    if env_sha:
        return env_sha
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _github_run_url() -> str:
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    run_id = os.getenv("GITHUB_RUN_ID", "").strip()
    return f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else ""


def _coverage_summary(coverage_json: Path | None, coverage_xml: Path | None) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if coverage_json is not None:
        data = json.loads(_read_text(coverage_json))
        totals = data.get("totals", {})
        summary["json"] = {
            "path": str(coverage_json),
            "sha256": _sha256(coverage_json),
            "covered_lines": totals.get("covered_lines"),
            "num_statements": totals.get("num_statements"),
            "percent_covered": totals.get("percent_covered"),
        }
    if coverage_xml is not None:
        summary["xml"] = {
            "path": str(coverage_xml),
            "sha256": _sha256(coverage_xml),
        }
    return summary


def _last_meaningful_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _log_summary(name: str, path: Path) -> dict[str, Any]:
    text = _read_text(path)
    pytest_match = re.search(r"=+ (?P<summary>[^=\n]*(?:passed|failed|error|skipped)[^=\n]*) =+", text)
    return {
        "name": name,
        "path": str(path),
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
        "summary": pytest_match.group("summary").strip() if pytest_match else _last_meaningful_line(text),
    }


def _parse_log_args(values: list[str]) -> list[tuple[str, Path]]:
    parsed: list[tuple[str, Path]] = []
    for value in values:
        if "=" not in value:
            raise RuntimeError(f"--log entries must use name=path form: {value}")
        name, raw_path = value.split("=", 1)
        name = name.strip()
        path = Path(raw_path)
        if not name:
            raise RuntimeError(f"--log entry has an empty name: {value}")
        if not path.is_file():
            raise RuntimeError(f"--log entry does not exist: {path}")
        parsed.append((name, path))
    return parsed


def build_manifest(
    root: Path,
    *,
    python_version: str,
    coverage_json: Path | None,
    coverage_xml: Path | None,
    logs: list[tuple[str, Path]],
) -> dict[str, Any]:
    return {
        "schema_version": "omnidesk-ci-evidence/v1",
        "project_version": _project_version(root),
        "source_commit": _git_commit(root),
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "github": {
            "repository": os.getenv("GITHUB_REPOSITORY", ""),
            "workflow": os.getenv("GITHUB_WORKFLOW", ""),
            "run_id": os.getenv("GITHUB_RUN_ID", ""),
            "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT", ""),
            "run_url": _github_run_url(),
            "job": os.getenv("GITHUB_JOB", ""),
            "ref": os.getenv("GITHUB_REF", ""),
            "event_name": os.getenv("GITHUB_EVENT_NAME", ""),
            "actor": os.getenv("GITHUB_ACTOR", ""),
        },
        "matrix": {
            "python_version": python_version or platform.python_version(),
        },
        "coverage": _coverage_summary(coverage_json, coverage_xml),
        "logs": [_log_summary(name, path) for name, path in logs],
        "policy": "This manifest binds the CI run, source commit, Python matrix cell, coverage files, and captured ruff/pyright/pytest logs for source-trunk evidence. It is not customer-distribution Real GA evidence.",
    }


def write_manifest(manifest: dict[str, Any], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a machine-readable GitHub Actions CI evidence manifest.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", required=True)
    parser.add_argument("--python-version", default="")
    parser.add_argument("--coverage-json")
    parser.add_argument("--coverage-xml")
    parser.add_argument("--log", action="append", default=[], help="Captured command log in name=path form; may be repeated.")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    coverage_json = Path(args.coverage_json) if args.coverage_json else None
    coverage_xml = Path(args.coverage_xml) if args.coverage_xml else None
    try:
        if coverage_json is not None and not coverage_json.is_file():
            raise RuntimeError(f"coverage JSON does not exist: {coverage_json}")
        if coverage_xml is not None and not coverage_xml.is_file():
            raise RuntimeError(f"coverage XML does not exist: {coverage_xml}")
        logs = _parse_log_args(args.log)
        manifest = build_manifest(
            root,
            python_version=args.python_version,
            coverage_json=coverage_json,
            coverage_xml=coverage_xml,
            logs=logs,
        )
        target = write_manifest(manifest, Path(args.output))
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"wrote CI evidence manifest: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
