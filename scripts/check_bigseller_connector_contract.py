#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REQUIRED_FILES = (
    "omnidesk_agent/integrations/bigseller/config.py",
    "omnidesk_agent/integrations/bigseller/idempotency.py",
    "omnidesk_agent/integrations/bigseller/errors.py",
    "omnidesk_agent/integrations/bigseller/webhooks.py",
    "omnidesk_agent/integrations/bigseller/worker.py",
    "omnidesk_agent/api/routes/bigseller.py",
    "docs/integrations/bigseller.md",
)

REQUIRED_SNIPPETS = {
    "omnidesk_agent/integrations/bigseller/config.py": (
        "state_backend",
        "postgres_dsn",
        "webhook_replay_window_seconds",
        "BIGSELLER_STATE_BACKEND=memory is not allowed",
    ),
    "omnidesk_agent/integrations/bigseller/idempotency.py": (
        "SQLiteBigSellerIdempotencyGuard",
        "PostgresBigSellerIdempotencyGuard",
        "create_bigseller_idempotency_guard",
        "FOR UPDATE",
    ),
    "omnidesk_agent/integrations/bigseller/errors.py": (
        "SQLiteBigSellerSyncErrorQueue",
        "PostgresBigSellerSyncErrorQueue",
        "create_bigseller_error_queue",
        "bigseller_sync_errors",
    ),
    "omnidesk_agent/integrations/bigseller/webhooks.py": (
        "verify_webhook_timestamp",
        "x-bigseller-event-id",
        "missing BigSeller webhook event id",
        "stale BigSeller webhook timestamp",
    ),
    "omnidesk_agent/integrations/bigseller/worker.py": (
        "bigseller_webhook_duplicate_total",
        "note_webhook_rejected",
        "create_bigseller_idempotency_guard",
        "create_bigseller_error_queue",
    ),
    "omnidesk_agent/api/routes/bigseller.py": (
        "note_webhook_rejected",
        "parse_bigseller_webhook",
    ),
    "docs/integrations/bigseller.md": (
        "BIGSELLER_STATE_BACKEND",
        "PostgreSQL",
        "replay protection",
        "live smoke evidence",
    ),
}

WORKFLOW_SNIPPETS = (
    "python scripts/check_bigseller_connector_contract.py .",
)


def _read(root: Path, rel: str) -> str:
    path = root / rel
    if not path.exists():
        raise FileNotFoundError(rel)
    return path.read_text(encoding="utf-8")


def _check(condition: bool, failures: list[str], message: str) -> None:
    if not condition:
        failures.append(message)


def audit(root: Path) -> dict[str, object]:
    failures: list[str] = []
    for rel in REQUIRED_FILES:
        _check((root / rel).exists(), failures, f"missing required file: {rel}")

    for rel, snippets in REQUIRED_SNIPPETS.items():
        try:
            text = _read(root, rel)
        except FileNotFoundError:
            continue
        for snippet in snippets:
            _check(snippet in text, failures, f"{rel} missing snippet: {snippet}")

    for workflow in (
        ".github/workflows/release-policy.yml",
        ".github/workflows/main-verification.yml",
    ):
        try:
            text = _read(root, workflow)
        except FileNotFoundError:
            failures.append(f"missing workflow: {workflow}")
            continue
        for snippet in WORKFLOW_SNIPPETS:
            _check(snippet in text, failures, f"{workflow} missing {snippet}")

    return {
        "schema": "omnidesk-bigseller-connector-contract/v1",
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "boundary": (
            "This source contract verifies durable state, replay protection, "
            "observability counters, and release-gate wiring. It does not claim "
            "live BigSeller production readiness without private API docs and live smoke evidence."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate BigSeller connector production hardening contract."
    )
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--write-report", default="")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    report = audit(root)
    if args.write_report:
        out = Path(args.write_report)
        if not out.is_absolute():
            out = root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {"status": report["status"], "failure_count": len(report["failures"])},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    for failure in report["failures"]:
        print(f"BLOCKER {failure}", file=sys.stderr)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
