#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omnidesk_agent.integrations.bigseller.client import HttpBigSellerClient
from omnidesk_agent.integrations.bigseller.config import BigSellerConfig
from omnidesk_agent.integrations.bigseller.idempotency import create_bigseller_idempotency_guard


REQUIRED_TRUE_FIELDS = (
    "auth_success",
    "order_list_success",
    "inventory_list_success",
    "webhook_signature_verified",
    "webhook_replay_guard_verified",
    "secret_leakage_checked",
    "no_secret_leakage",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for BigSeller live smoke")
    return value


def _bool_true(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"true", "yes", "1", "ok", "passed", "verified"}


def validate(evidence: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if evidence.get("schema") != "omnidesk-bigseller-live-smoke/v1":
        issues.append("schema must be omnidesk-bigseller-live-smoke/v1")
    if str(evidence.get("status") or "").strip().lower() not in {"passed", "verified", "success", "succeeded", "ok"}:
        issues.append("status must be passed/verified")
    for field in ("produced_at", "producer", "environment", "store_id", "trace_id", "audit_event_id"):
        if not str(evidence.get(field) or "").strip():
            issues.append(f"{field} is required")
    for field in REQUIRED_TRUE_FIELDS:
        if not _bool_true(evidence.get(field)):
            issues.append(f"{field} must be true")
    try:
        latency = float(evidence.get("p95_latency_ms"))
        if latency <= 0 or latency > 10000:
            issues.append("p95_latency_ms must be > 0 and <= 10000")
    except Exception:
        issues.append("p95_latency_ms must be numeric")
    try:
        error_rate = float(evidence.get("error_rate"))
        if error_rate < 0 or error_rate > 0.01:
            issues.append("error_rate must be between 0 and 0.01")
    except Exception:
        issues.append("error_rate must be numeric")
    return issues


def _probe(client: HttpBigSellerClient, path: str) -> tuple[Any, float]:
    started = time.perf_counter()
    result = client.request("GET", path)
    latency_ms = max(0.001, (time.perf_counter() - started) * 1000.0)
    if result in ({}, [], None, ""):
        raise RuntimeError(f"BigSeller probe returned empty response for {path}")
    return result, latency_ms


def _secret_values(config: BigSellerConfig) -> list[str]:
    values = (config.app_key, config.access_token, config.refresh_token, config.webhook_secret, config.auth_code)
    return [str(value) for value in values if value and len(str(value)) >= 6]


def _webhook_delivery_verified(config: BigSellerConfig) -> bool:
    """True only if a real vendor webhook was accepted and is now refused as a replay.

    The gateway claims a webhook event id only after its signature verified,
    so a durable claim for BIGSELLER_LIVE_WEBHOOK_EVENT_ID proves a signed
    delivery; the claim refusing a second time proves the replay guard.
    """
    event_id = os.getenv("BIGSELLER_LIVE_WEBHOOK_EVENT_ID", "").strip()
    if not event_id:
        return False
    store_id = os.getenv("BIGSELLER_LIVE_WEBHOOK_STORE_ID", "").strip() or "__unknown_store__"
    guard = create_bigseller_idempotency_guard(config)
    if guard.claim(external_id=event_id, store_id=store_id, action_type="webhook_event"):
        # No such delivery was recorded; undo the probe claim.
        guard.release(external_id=event_id, store_id=store_id, action_type="webhook_event")
        return False
    return True


def build_evidence(root: Path) -> dict[str, Any]:
    config = BigSellerConfig.from_env(workspace_root=root)
    if not config.enabled or config.use_mock:
        raise RuntimeError("BigSeller live smoke requires BIGSELLER_ENABLED=true and BIGSELLER_USE_MOCK=false")
    issues = config.real_mode_issues()
    if issues:
        raise RuntimeError("; ".join(issues))

    auth_probe_path = _required_env("BIGSELLER_LIVE_AUTH_PROBE_PATH")
    order_list_path = _required_env("BIGSELLER_LIVE_ORDER_LIST_PATH")
    inventory_list_path = _required_env("BIGSELLER_LIVE_INVENTORY_LIST_PATH")
    store_id = _required_env("BIGSELLER_LIVE_SMOKE_STORE_ID")
    trace_id = _required_env("BIGSELLER_LIVE_SMOKE_TRACE_ID")
    audit_event_id = _required_env("BIGSELLER_LIVE_SMOKE_AUDIT_EVENT_ID")

    client = HttpBigSellerClient(config)
    responses: list[Any] = []
    latencies: list[float] = []
    for path in (auth_probe_path, order_list_path, inventory_list_path):
        result, latency_ms = _probe(client, path)
        responses.append(result)
        latencies.append(latency_ms)
    webhook_verified = _webhook_delivery_verified(config)

    evidence = {
        "schema": "omnidesk-bigseller-live-smoke/v1",
        "status": "passed",
        "produced_at": _utc_now(),
        "producer": os.getenv("GITHUB_WORKFLOW", "manual-approved-bigseller-live-smoke"),
        "environment": os.getenv("BIGSELLER_LIVE_SMOKE_ENVIRONMENT", "staging"),
        "store_id": store_id,
        "auth_success": True,
        "order_list_success": True,
        "inventory_list_success": True,
        "webhook_signature_verified": webhook_verified,
        "webhook_replay_guard_verified": webhook_verified,
        "trace_id": trace_id,
        "audit_event_id": audit_event_id,
        # Three probes: the slowest one stands in for p95, never their sum.
        "p95_latency_ms": round(max(latencies), 3),
        "latency_samples": len(latencies),
        # A failed probe raises before evidence is written, so a written file
        # always has zero failed probes out of len(latencies).
        "error_rate": 0.0,
        "probes_failed": 0,
    }
    secrets = _secret_values(config)
    serialized = json.dumps({"responses": responses, "evidence": evidence}, default=str, ensure_ascii=False)
    evidence["secret_leakage_checked"] = bool(secrets)
    evidence["no_secret_leakage"] = bool(secrets) and not any(secret in serialized for secret in secrets)
    validation_issues = validate(evidence)
    if validation_issues:
        raise RuntimeError("BigSeller live smoke evidence failed validation: " + "; ".join(validation_issues))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run BigSeller live smoke against approved real endpoints.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="release/external-evidence/integrations/bigseller-live-smoke.json")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = root / output_path
    try:
        evidence = build_evidence(root)
    except Exception as exc:
        print(f"BigSeller live smoke failed: {exc}", file=sys.stderr)
        return 1
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "passed", "output": str(output_path)}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
