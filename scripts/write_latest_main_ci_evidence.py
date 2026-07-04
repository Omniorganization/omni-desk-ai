#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

REQUIRED_CHECKS = [
    "CI",
    "Tri-App Quality Gate",
    "Security",
    "Supply Chain",
    "Release",
]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def build_evidence(source_commit: str, ref_name: str, run_id: str) -> dict:
    repository = _env("GITHUB_REPOSITORY")
    server = _env("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    run_url = f"{server}/{repository}/actions/runs/{run_id}" if repository and run_id else ""
    return {
        "schema": "omnidesk-latest-main-ci-evidence/v1",
        "status": "passed",
        "produced_at": int(time.time()),
        "producer": "github-actions",
        "repository": repository,
        "ref": ref_name,
        "source_commit": source_commit,
        "workflow_run_id": run_id,
        "workflow_run_url": run_url,
        "required_checks": REQUIRED_CHECKS,
        "notes": [
            "This artifact is produced by the latest-main evidence workflow.",
            "It records the commit and workflow run that generated source-side CI evidence.",
            "Customer-distribution Real GA still requires scripts/check_external_ga_evidence.py without --audit-only.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write latest-main CI evidence JSON.")
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-commit", default=_env("GITHUB_SHA"))
    parser.add_argument("--ref-name", default=_env("GITHUB_REF_NAME"))
    parser.add_argument("--run-id", default=_env("GITHUB_RUN_ID"))
    args = parser.parse_args(argv)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = build_evidence(args.source_commit, args.ref_name, args.run_id)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    print(f"latest-main CI evidence written: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
