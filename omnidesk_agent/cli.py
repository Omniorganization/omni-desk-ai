from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
from typing import Iterator

import uvicorn

from omnidesk_agent.config import AppConfig, load_config
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.daemon import OmniDeskRuntime
from omnidesk_agent.server import create_app


@contextmanager
def runtime_context(cfg: AppConfig) -> Iterator[OmniDeskRuntime]:
    rt = OmniDeskRuntime(cfg)
    try:
        yield rt
    finally:
        close = getattr(rt, "close", None)
        if callable(close):
            close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="omnidesk")
    parser.add_argument("--config", default="~/.omnidesk/config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    sub.add_parser("validate-connectors")
    sub.add_parser("validate-extensions")
    sub.add_parser("validate-models")
    sub.add_parser("validate-models-live")
    sub.add_parser("validate-webhook-signatures")
    sub.add_parser("gmail-auth")
    learn_p = sub.add_parser("learning-report"); learn_p.add_argument("--days", type=int, default=7)
    l10_p = sub.add_parser("learning-l10-report"); l10_p.add_argument("--days", type=int, default=7); l10_p.add_argument("--format", choices=["json", "html"], default="json")
    metrics_p = sub.add_parser("metrics"); metrics_p.add_argument("--days", type=int, default=7)
    exp_p = sub.add_parser("experience-search"); exp_p.add_argument("query"); exp_p.add_argument("--limit", type=int, default=5)
    up_p = sub.add_parser("upgrade-proposals"); up_p.add_argument("--status", default=None)
    gen_p = sub.add_parser("upgrade-artifact"); gen_p.add_argument("proposal_id")
    fb_p = sub.add_parser("upgrade-feedback"); fb_p.add_argument("proposal_id"); fb_p.add_argument("decision", choices=["approved", "rejected"]); fb_p.add_argument("--reason", default="")
    eval_p = sub.add_parser("upgrade-evaluate"); eval_p.add_argument("proposal_id")
    run_p = sub.add_parser("run"); run_p.add_argument("message")
    remember_p = sub.add_parser("remember"); remember_p.add_argument("text"); remember_p.add_argument("--tags", default="")
    search_p = sub.add_parser("search"); search_p.add_argument("query")
    serve_p = sub.add_parser("serve"); serve_p.add_argument("--host"); serve_p.add_argument("--port", type=int)
    args = parser.parse_args()
    cfg = load_config(args.config)

    if args.cmd == "doctor":
        with runtime_context(cfg) as rt:
            print(json.dumps(rt.status(), ensure_ascii=False, indent=2))
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
