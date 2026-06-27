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
    "omnidesk_agent/observability/metrics.py",
    "omnidesk_agent/api/routes/bigseller.py",
    "scripts/run_bigseller_live_smoke.py",
    "scripts/import_bigseller_live_smoke_evidence.py",
    "docs/integrations/bigseller.md",
)

REQUIRED_SNIPPETS = {
    "omnidesk_agent/integrations/bigseller/config.py": (
        "state_backend",
        "postgres_dsn",
        "webhook_replay_window_seconds",
        "webhook_event_ttl_seconds",
        "webhook_max_body_bytes",
        "BIGSELLER_WEBHOOK_MAX_BODY_BYTES",
        "BIGSELLER_STATE_BACKEND=memory is not allowed",
    ),
    "omnidesk_agent/integrations/bigseller/idempotency.py": (
        "SQLiteBigSellerIdempotencyGuard",
        "PostgresBigSellerIdempotencyGuard",
        "create_bigseller_idempotency_guard",
        "purge_expired",
        "expires_at",
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
        "MetricsRegistry",
        "omnidesk_bigseller_webhook_duplicate_total",
        "omnidesk_bigseller_dead_letter_current",
        "prometheus_metrics",
        "bigseller_webhook_duplicate_total",
        "bigseller_dead_letter_current",
        "note_webhook_rejected",
        "create_bigseller_idempotency_guard",
        "create_bigseller_error_queue",
    ),
    "omnidesk_agent/observability/metrics.py": (
        "class MetricsRegistry",
        "render_prometheus",
        "omnidesk_",
    ),
    "omnidesk_agent/api/routes/bigseller.py": (
        "webhook_max_body_bytes",
        "payload too large",
        "status_code=413",
        "note_webhook_rejected",
        "parse_bigseller_webhook",
    ),
    "scripts/run_bigseller_live_smoke.py": (
        "BIGSELLER_USE_MOCK=false",
        "BIGSELLER_LIVE_ORDER_LIST_PATH",
        "BIGSELLER_LIVE_INVENTORY_LIST_PATH",
        "validate(evidence)",
    ),
    "scripts/import_bigseller_live_smoke_evidence.py": (
        "omnidesk-bigseller-live-smoke/v1",
        "no_secret_leakage",
        "PLACEHOLDER_RE",
        "SECRET_RE",
    ),
    "docs/integrations/bigseller.md": (
        "BIGSELLER_STATE_BACKEND",
        "BIGSELLER_WEBHOOK_MAX_BODY_BYTES",
        "PostgreSQL",
        "replay protection",
        "live smoke evidence",
    ),
}

WORKFLOW_SNIPPETS = (
    "python scripts/check_bigseller_connector_contract.py .",
)

EXTERNAL_EVIDENCE_SNIPPETS = (
    "bigseller_live_smoke",
    "integrations/bigseller-live-smoke.json",
)

MANIFEST_ONLY_SNIPPETS = (
    "run_bigseller_live_smoke.py",
    "import_bigseller_live_smoke_evidence.py",
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

    for rel in (
        "scripts/check_external_ga_evidence.py",
        "release/production-evidence.manifest.json",
        "release/external-ga-evidence.required.json",
    ):
        try:
            text = _read(root, rel)
        except FileNotFoundError:
            failures.append(f"missing external evidence contract file: {rel}")
            continue
        for snippet in EXTERNAL_EVIDENCE_SNIPPETS:
            _check(snippet in text, failures, f"{rel} missing {snippet}")

    try:
        manifest = _read(root, "release/production-evidence.manifest.json")
        for snippet in MANIFEST_ONLY_SNIPPETS:
            _check(snippet in manifest, failures, f"production evidence manifest missing {snippet}")
    except FileNotFoundError:
        failures.append("missing production evidence manifest")

    return {
        "schema": "omnidesk-bigseller-connector-contract/v3",
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "boundary": (
            "This source contract verifies durable state, replay protection, "
            "body-size enforcement, TTL purge, observability registry wiring, release-gate wiring, "
            "live-smoke runner/importer, and BigSeller external evidence gating. It does not claim "
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
