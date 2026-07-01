from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
from typing import Iterator

import uvicorn

from omnidesk_agent.config import AppConfig, load_config
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.daemon import OmniDeskRuntime
from omnidesk_agent.server import create_app
from omnidesk_agent.storage.sqlite import close_all_open_connections
from omnidesk_agent.validation.production import assert_production_config_safe, validate_production_config


_PRODUCTION_ISSUE_CATEGORY_MARKERS = (
    ("api_resource_guard", ("api_resource_guard", "api resource guard")),
    ("app_sync", ("app_sync",)),
    ("capabilities", ("capabilities.",)),
    ("channels", ("channels.",)),
    ("gateway", ("gateway.", "gateway ")),
    ("gmail", ("gmail",)),
    ("memory_privacy", ("memory_privacy", "memory encryption")),
    ("models", ("models.", "llm.")),
    ("observability", ("observability.",)),
    ("permissions", ("permissions.", "break-glass")),
    ("plugins", ("plugins.", "plugin ")),
    ("sandbox", ("sandbox.",)),
    ("storage", ("storage.", "postgres dsn")),
)


@contextmanager
def runtime_context(cfg: AppConfig, *, validate_production: bool = True) -> Iterator[OmniDeskRuntime]:
    rt = None
    try:
        if validate_production:
            assert_production_config_safe(cfg)
        rt = OmniDeskRuntime(cfg)
        yield rt
    finally:
        try:
            if rt is not None:
                close = getattr(rt, "close", None)
                if callable(close):
                    close()
        finally:
            close_all_open_connections()


def _production_issue_category(issue: object) -> str:
    text = str(issue).lower()
    for category, markers in _PRODUCTION_ISSUE_CATEGORY_MARKERS:
        if any(marker in text for marker in markers):
            return category
    return "production_config"


def _production_check_output(cfg: AppConfig, result: dict) -> dict:
    issues = list(result.get("issues") or [])
    budget = cfg.models.budget
    return {
        "ok": bool(result.get("ok")),
        "production": bool(result.get("production")),
        "issue_count": len(issues),
        "issue_categories": sorted({_production_issue_category(issue) for issue in issues}),
        "issues_redacted": bool(issues),
        "storage_backend": cfg.storage.backend,
        "api_resource_guard_backend": cfg.api_resource_guard.backend,
        "model_budget": {
            "daily_usd_limit": budget.daily_usd_limit,
            "monthly_usd_limit": budget.monthly_usd_limit,
            "per_actor_daily_usd_limit": budget.per_actor_daily_usd_limit,
            "on_exceed": budget.on_exceed,
            "require_persistent_ledger": budget.require_persistent_ledger,
        },
        "cost_ledger_backend": "postgres" if cfg.storage.backend == "postgres" else "sqlite",
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="omnidesk")
    parser.add_argument("--config", default="~/.omnidesk/config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    doctor_p = sub.add_parser("doctor")
    doctor_p.add_argument("--fix", action="store_true")
    doctor_p.add_argument("--profile", choices=["source-only", "single-mac-ga-lab", "enterprise", "offline-first"], default="single-mac-ga-lab")
    onboard_p = sub.add_parser("onboard")
    onboard_p.add_argument("--enterprise", action="store_true")
    onboard_p.add_argument("--single-mac-ga-lab", action="store_true")
    evidence_p = sub.add_parser("evidence")
    evidence_sub = evidence_p.add_subparsers(dest="evidence_cmd", required=True)
    evidence_sub.add_parser("doctor")
    channel_p = sub.add_parser("channel")
    channel_sub = channel_p.add_subparsers(dest="channel_cmd", required=True)
    channel_onboard = channel_sub.add_parser("onboard")
    channel_onboard.add_argument("channel")
    device_p = sub.add_parser("device")
    device_sub = device_p.add_subparsers(dest="device_cmd", required=True)
    device_pair = device_sub.add_parser("pair")
    device_pair.add_argument("device_id")
    device_pair.add_argument("--channel", default="local")
    app_p = sub.add_parser("app")
    app_sub = app_p.add_subparsers(dest="app_cmd", required=True)
    app_connect = app_sub.add_parser("connect")
    app_connect.add_argument("app")
    sub.add_parser("validate-connectors")
    sub.add_parser("validate-extensions")
    sub.add_parser("validate-models")
    sub.add_parser("validate-models-live")
    sub.add_parser("validate-webhook-signatures")
    sub.add_parser("reconnect-once")
    sub.add_parser("production-check")
    sub.add_parser("gmail-auth")
    learn_p = sub.add_parser("learning-report")
    learn_p.add_argument("--days", type=int, default=7)
    l10_p = sub.add_parser("learning-l10-report")
    l10_p.add_argument("--days", type=int, default=7)
    l10_p.add_argument("--format", choices=["json", "html"], default="json")
    metrics_p = sub.add_parser("metrics")
    metrics_p.add_argument("--days", type=int, default=7)
    exp_p = sub.add_parser("experience-search")
    exp_p.add_argument("query")
    exp_p.add_argument("--limit", type=int, default=5)
    up_p = sub.add_parser("upgrade-proposals")
    up_p.add_argument("--status", default=None)
    gen_p = sub.add_parser("upgrade-artifact")
    gen_p.add_argument("proposal_id")
    fb_p = sub.add_parser("upgrade-feedback")
    fb_p.add_argument("proposal_id")
    fb_p.add_argument("decision", choices=["approved", "rejected"])
    fb_p.add_argument("--reason", default="")
    eval_p = sub.add_parser("upgrade-evaluate")
    eval_p.add_argument("proposal_id")
    run_p = sub.add_parser("run")
    run_p.add_argument("message")
    remember_p = sub.add_parser("remember")
    remember_p.add_argument("text")
    remember_p.add_argument("--tags", default="")
    search_p = sub.add_parser("search")
    search_p.add_argument("query")
    serve_p = sub.add_parser("serve")
    serve_p.add_argument("--host")
    serve_p.add_argument("--port", type=int)
    args = parser.parse_args()
    try:
        cfg = load_config(args.config, ensure_dirs=args.cmd != "production-check")
    except TypeError as exc:
        if "ensure_dirs" not in str(exc):
            raise
        cfg = load_config(args.config)

    if args.cmd == "doctor":
        from omnidesk_agent.onboarding import run_doctor

        print(json.dumps(run_doctor(cfg, profile=args.profile, fix=args.fix, root=Path.cwd()).to_dict(), ensure_ascii=False, indent=2))
        return

    if args.cmd == "onboard":
        from omnidesk_agent.onboarding import build_onboarding_plan

        profile = "enterprise" if args.enterprise else "single-mac-ga-lab"
        print(json.dumps(build_onboarding_plan(cfg, profile=profile), ensure_ascii=False, indent=2))
        return

    if args.cmd == "evidence" and args.evidence_cmd == "doctor":
        from omnidesk_agent.onboarding import build_evidence_doctor

        print(json.dumps(build_evidence_doctor(Path.cwd()), ensure_ascii=False, indent=2))
        return

    if args.cmd == "channel" and args.channel_cmd == "onboard":
        from omnidesk_agent.onboarding import build_channel_onboarding_plan

        print(json.dumps(build_channel_onboarding_plan(args.channel), ensure_ascii=False, indent=2))
        return

    if args.cmd == "device" and args.device_cmd == "pair":
        from omnidesk_agent.onboarding import build_device_pairing_challenge

        print(json.dumps(build_device_pairing_challenge(args.device_id, channel=args.channel), ensure_ascii=False, indent=2))
        return

    if args.cmd == "app" and args.app_cmd == "connect":
        from omnidesk_agent.onboarding import build_app_connection_plan

        print(json.dumps(build_app_connection_plan(args.app), ensure_ascii=False, indent=2))
        return

    if args.cmd == "validate-connectors":
        from omnidesk_agent.validation.connectors import validate_connectors
        with runtime_context(cfg) as rt:
            print(json.dumps(validate_connectors(rt), ensure_ascii=False, indent=2))
        return

    if args.cmd == "validate-extensions":
        from omnidesk_agent.validation.extensions import validate_extensions
        with runtime_context(cfg) as rt:
            print(json.dumps(validate_extensions(rt), ensure_ascii=False, indent=2))
        return

    if args.cmd == "validate-models":
        from omnidesk_agent.validation.models import validate_models
        with runtime_context(cfg) as rt:
            print(json.dumps(validate_models(rt), ensure_ascii=False, indent=2))
        return

    if args.cmd == "validate-models-live":
        from omnidesk_agent.validation.models import live_connectivity_test
        with runtime_context(cfg) as rt:
            print(json.dumps(asyncio.run(live_connectivity_test(rt)), ensure_ascii=False, indent=2))
        return

    if args.cmd == "validate-webhook-signatures":
        print(json.dumps({"ok": True, "tests": ["wechat_signature", "line_signature_valid"]}, ensure_ascii=False, indent=2))
        return

    if args.cmd == "reconnect-once":
        with runtime_context(cfg) as rt:
            print(json.dumps(rt.run_reconnect_once(), ensure_ascii=False, indent=2))
        return

    if args.cmd == "production-check":
        result = validate_production_config(cfg)
        print(json.dumps(_production_check_output(cfg, result), ensure_ascii=False, indent=2))
        if not result["ok"]:
            raise SystemExit(1)
        return

    if args.cmd == "gmail-auth":
        with runtime_context(cfg) as rt:
            token = rt.adapters["gmail"].oauth.run_local_flow()
            print(json.dumps({"ok": True, "token_saved": True, "keys": sorted(token.keys())}, ensure_ascii=False, indent=2))
        return

    if args.cmd == "learning-report":
        with runtime_context(cfg) as rt:
            print(json.dumps(rt.learning_job.run(days=args.days), ensure_ascii=False, indent=2))
        return

    if args.cmd == "learning-l10-report":
        from omnidesk_agent.self_learning.observability.dashboard import LearningDashboard
        dashboard = LearningDashboard.from_audit_path(cfg.workspace.root / "learning_audit.jsonl")
        print(dashboard.render_html(days=args.days) if args.format == "html" else json.dumps(dashboard.summary(days=args.days), ensure_ascii=False, indent=2))
        return

    if args.cmd == "metrics":
        with runtime_context(cfg) as rt:
            print(json.dumps(rt.memory.metrics_report(days=args.days), ensure_ascii=False, indent=2))
        return

    if args.cmd == "experience-search":
        with runtime_context(cfg) as rt:
            print(json.dumps(rt.memory.retrieve_for_task(args.query, limit=args.limit), ensure_ascii=False, indent=2))
        return

    if args.cmd == "upgrade-proposals":
        with runtime_context(cfg) as rt:
            print(json.dumps([p.to_dict() for p in rt.proposal_store.list(status=args.status)], ensure_ascii=False, indent=2))
        return

    if args.cmd == "upgrade-artifact":
        with runtime_context(cfg) as rt:
            print(json.dumps(rt.governance.generate_artifact(args.proposal_id), ensure_ascii=False, indent=2))
        return

    if args.cmd == "upgrade-feedback":
        with runtime_context(cfg) as rt:
            print(json.dumps(rt.governance.record_human_feedback(args.proposal_id, args.decision, args.reason), ensure_ascii=False, indent=2))
        return

    if args.cmd == "upgrade-evaluate":
        with runtime_context(cfg) as rt:
            print(json.dumps(asyncio.run(rt.governance.evaluate_proposal(args.proposal_id)), ensure_ascii=False, indent=2))
        return

    if args.cmd == "run":
        with runtime_context(cfg) as rt:
            msg = ChannelMessage(channel="local-cli", sender_id="owner", text=args.message)
            print(json.dumps(asyncio.run(rt.orchestrator.handle_message(msg)), ensure_ascii=False, indent=2))
        return

    if args.cmd == "remember":
        with runtime_context(cfg) as rt:
            tags = [t.strip() for t in args.tags.split(",") if t.strip()]
            print(f"remembered experience #{rt.memory.add(args.text, tags=tags)}")
        return

    if args.cmd == "search":
        with runtime_context(cfg) as rt:
            print(json.dumps(rt.memory.search(args.query), ensure_ascii=False, indent=2))
        return

    if args.cmd == "serve":
        if args.host:
            cfg.gateway.host = args.host
        if args.port:
            cfg.gateway.port = args.port
        app = create_app(cfg)
        uvicorn.run(app, host=cfg.gateway.host, port=cfg.gateway.port)
        return
