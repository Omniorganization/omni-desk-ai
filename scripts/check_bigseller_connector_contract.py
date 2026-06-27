#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REQUIRED_FILES = (
    "omnidesk_agent/integrations/bigseller/config.py",
    "omnidesk_agent/integrations/bigseller/client.py",
    "omnidesk_agent/integrations/bigseller/idempotency.py",
    "omnidesk_agent/integrations/bigseller/errors.py",
    "omnidesk_agent/integrations/bigseller/webhooks.py",
    "omnidesk_agent/integrations/bigseller/worker.py",
    "omnidesk_agent/observability/metrics.py",
    "omnidesk_agent/api/routes/bigseller.py",
    "scripts/check_bigseller_route_enablement.py",
    "scripts/run_bigseller_live_smoke.py",
    "scripts/import_bigseller_live_smoke_evidence.py",
    "docs/integrations/bigseller.md",
    ".github/workflows/release-policy.yml",
    ".github/workflows/main-verification.yml",
    ".github/workflows/bigseller-real-contract.yml",
    "release/production-evidence.manifest.json",
    "release/external-ga-evidence.required.json",
)

HARD_REQUIRED_SNIPPETS = {
    "omnidesk_agent/integrations/bigseller/config.py": (
        "state_backend",
        "postgres_dsn",
        "webhook_max_body_bytes",
        "BIGSELLER_STATE_BACKEND=memory is not allowed",
        "BIGSELLER_ORDERS_LIST_PATH",
        "BIGSELLER_FULFILLMENT_SYNC_PATH",
        "BIGSELLER_REQUEST_SIGNING_ENABLED",
        "real_endpoint_contract_configured",
    ),
    "omnidesk_agent/integrations/bigseller/client.py": (
        "class HttpBigSellerClient",
        "request_signing_enabled",
        "_signed_headers",
        "exchange_auth_code",
        "refresh_access_token",
        "list_orders",
        "list_inventory",
        "sync_fulfillment_status",
    ),
    "omnidesk_agent/integrations/bigseller/idempotency.py": (
        "SQLiteBigSellerIdempotencyGuard",
        "PostgresBigSellerIdempotencyGuard",
        "create_bigseller_idempotency_guard",
        "purge_expired",
    ),
    "omnidesk_agent/integrations/bigseller/errors.py": (
        "SQLiteBigSellerSyncErrorQueue",
        "PostgresBigSellerSyncErrorQueue",
        "create_bigseller_error_queue",
        "bigseller_sync_errors",
    ),
    "omnidesk_agent/integrations/bigseller/webhooks.py": (
        "verify_webhook_timestamp",
        "missing BigSeller webhook event id",
        "stale BigSeller webhook timestamp",
    ),
    "omnidesk_agent/integrations/bigseller/worker.py": (
        "MetricsRegistry",
        "create_bigseller_idempotency_guard",
        "create_bigseller_error_queue",
        "def list_errors",
        "def retry_error",
        "def resolve_error",
    ),
    "omnidesk_agent/api/routes/bigseller.py": (
        "webhook_max_body_bytes",
        "status_code=413",
        "parse_bigseller_webhook",
        "/errors/{error_id}/retry",
        "/errors/{error_id}/resolve",
    ),
    "scripts/check_bigseller_route_enablement.py": (
        "BIGSELLER_REGISTER_ROUTES",
        "def audit",
        "omnidesk-bigseller-route-enablement/",
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
        "BIGSELLER_REGISTER_ROUTES",
        "Real Adapter Contract",
        "live smoke evidence",
        "GET /integrations/bigseller/errors",
        "POST /integrations/bigseller/errors/{error_id}/retry",
    ),
    ".github/workflows/release-policy.yml": (
        "python scripts/check_bigseller_connector_contract.py .",
        "python scripts/check_bigseller_route_enablement.py .",
    ),
    ".github/workflows/main-verification.yml": (
        "python scripts/check_bigseller_connector_contract.py .",
    ),
    ".github/workflows/bigseller-real-contract.yml": (
        "BigSeller Real Contract",
        "check_bigseller_route_enablement.py",
        "run_bigseller_live_smoke.py",
        "real-ga",
        "candidate",
    ),
    "release/production-evidence.manifest.json": (
        "bigseller_live_smoke",
        "bigseller_route_enablement",
        "bigseller_real_contract_workflow",
        "run_bigseller_live_smoke.py",
    ),
    "release/external-ga-evidence.required.json": (
        "bigseller_live_smoke",
        "integrations/bigseller-live-smoke.json",
    ),
}

SOFT_RECOMMENDED_SNIPPETS = {
    "omnidesk_agent/integrations/bigseller/worker.py": (
        "omnidesk_bigseller_webhook_duplicate_total",
        "omnidesk_bigseller_dead_letter_current",
        "prometheus_metrics",
    ),
    "docs/integrations/bigseller.md": (
        "BIGSELLER_ENABLED does not register routes",
        "BIGSELLER_RESPONSE_ROOT_KEYS",
        "BIGSELLER_REQUEST_SIGNING_ENABLED",
    ),
}


def _read(root: Path, rel: str) -> str:
    path = root / rel
    if not path.exists():
        raise FileNotFoundError(rel)
    return path.read_text(encoding="utf-8")


def _check_snippets(
    root: Path,
    mapping: dict[str, tuple[str, ...]],
    *,
    severity: str,
) -> list[str]:
    issues: list[str] = []
    for rel, snippets in mapping.items():
        try:
            text = _read(root, rel)
        except FileNotFoundError:
            issues.append(f"{severity}: missing file while checking snippets: {rel}")
            continue
        for snippet in snippets:
            if snippet not in text:
                issues.append(f"{severity}: {rel} missing snippet: {snippet}")
    return issues


def audit(root: Path) -> dict[str, object]:
    failures: list[str] = []
    warnings: list[str] = []
    for rel in REQUIRED_FILES:
        if not (root / rel).exists():
            failures.append(f"missing required file: {rel}")

    failures.extend(_check_snippets(root, HARD_REQUIRED_SNIPPETS, severity="BLOCKER"))
    warnings.extend(_check_snippets(root, SOFT_RECOMMENDED_SNIPPETS, severity="WARNING"))

    return {
        "schema": "omnidesk-bigseller-connector-contract/v7",
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "warnings": warnings,
        "boundary": (
            "This source contract blocks missing BigSeller production-critical files and capabilities, "
            "including durable state, replay protection, body-size enforcement, real adapter wiring, "
            "explicit route enablement, Admin retry/dead-letter operations, release-gate wiring, and live-smoke evidence hooks. "
            "Versioned wording differences are warnings, not blockers. The contract does not claim live BigSeller production readiness without private API docs and live smoke evidence."
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
            {
                "status": report["status"],
                "failure_count": len(report["failures"]),
                "warning_count": len(report["warnings"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    for failure in report["failures"]:
        print(f"BLOCKER {failure}", file=sys.stderr)
    for warning in report["warnings"]:
        print(warning, file=sys.stderr)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
