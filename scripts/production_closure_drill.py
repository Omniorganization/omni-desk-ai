#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Allow running as `python scripts/production_closure_drill.py` from a source tree
# before editable installation.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from omnidesk_agent.config import AppConfig, load_config  # noqa: E402
from omnidesk_agent.observability_probe import probe_otlp_endpoint  # noqa: E402
from omnidesk_agent.repositories.health import check_repository_factory  # noqa: E402
from omnidesk_agent.repositories.runtime import build_repository_factory, storage_plan  # noqa: E402
from omnidesk_agent.security.audit_worm import WormAuditCheckpoint  # noqa: E402
from scripts.check_kubernetes_contract import main as check_kubernetes_contract_main  # noqa: E402


def _record(ok: bool, **fields: Any) -> dict[str, Any]:
    return {"ok": bool(ok), **fields}


def _config(args: argparse.Namespace) -> AppConfig:
    if args.config:
        return load_config(args.config)
    cfg = AppConfig()
    if args.backend:
        cfg.storage.backend = args.backend
    if args.require_multi_instance_safe:
        cfg.storage.require_multi_instance_safe = True
    return cfg


def run_contract_only(root: Path) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    kube_rc = check_kubernetes_contract_main([str(root)])
    checks.append(_record(kube_rc == 0, name="kubernetes_contract"))
    required = [
        root / "scripts" / "check_enterprise_readiness.py",
        root / "scripts" / "check_deployment_readiness.py",
        root / "scripts" / "production_smoke_test.py",
        root / "scripts" / "check_audit_checkpoint.py",
        root / "omnidesk_agent" / "observability_probe.py",
        root / "omnidesk_agent" / "repositories" / "health.py",
        root / "omnidesk_agent" / "security" / "break_glass.py",
        root / "docs" / "ENTERPRISE_GA14_PRODUCTION_CLOSURE.md",
    ]
    missing = [str(path.relative_to(root)) for path in required if not path.exists()]
    checks.append(_record(not missing, name="closure_assets", missing=missing))
    return checks


def run_live(args: argparse.Namespace) -> list[dict[str, Any]]:
    cfg = _config(args)
    checks: list[dict[str, Any]] = []
    try:
        plan = storage_plan(backend=cfg.storage.backend, require_multi_instance_safe=cfg.storage.require_multi_instance_safe)
        checks.append(_record(True, name="storage_plan", **plan.__dict__))
        factory = build_repository_factory(backend=cfg.storage.backend, workspace_root=cfg.workspace.root, postgres_dsn_env=cfg.storage.postgres_dsn_env)
        health = check_repository_factory(factory, require_multi_instance_safe=cfg.storage.require_multi_instance_safe, live_write=args.live_write)
        checks.append(_record(health.ok, name="storage_health", **health.to_dict()))
    except Exception as exc:
        checks.append(_record(False, name="storage_health", error=str(exc)))

    endpoint = args.otlp_endpoint or os.getenv(cfg.observability.otlp_endpoint_env, "")
    if endpoint:
        probe = probe_otlp_endpoint(endpoint, timeout=cfg.observability.otlp_timeout_seconds)
        checks.append(_record(probe.ok, name="otlp_collector", **probe.to_dict()))
    else:
        checks.append(_record(False, name="otlp_collector", error=f"{cfg.observability.otlp_endpoint_env} not configured"))

    if args.audit_log:
        try:
            checkpoint = WormAuditCheckpoint(Path(args.audit_checkpoint_dir), hmac_key_env=cfg.permissions.audit_checkpoint_hmac_key_env)
            created = checkpoint.create(Path(args.audit_log), label="production-closure-drill")
            checks.append(_record(True, name="audit_checkpoint", audit_log_sha256=created.audit_log_sha256, checkpoint_hash=created.checkpoint_hash, signed=bool(created.signature)))
        except Exception as exc:
            checks.append(_record(False, name="audit_checkpoint", error=str(exc)))
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run production closure drills for storage, observability, Kubernetes, and audit checkpointing.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--config")
    parser.add_argument("--backend", choices=["sqlite", "postgres"])
    parser.add_argument("--require-multi-instance-safe", action="store_true")
    parser.add_argument("--live-write", action="store_true")
    parser.add_argument("--otlp-endpoint")
    parser.add_argument("--audit-log")
    parser.add_argument("--audit-checkpoint-dir", default=".omnidesk-audit-checkpoints")
    parser.add_argument("--contract-only", action="store_true", help="Validate package closure assets without external services.")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    checks = run_contract_only(root)
    if not args.contract_only:
        checks.extend(run_live(args))
    ok = all(item.get("ok") for item in checks)
    report = {"ok": ok, "checks": checks}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
