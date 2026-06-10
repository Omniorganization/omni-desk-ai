from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import uvicorn

from omnidesk_agent.config import load_config
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.daemon import OmniDeskRuntime
from omnidesk_agent.server import create_app
from omnidesk_agent.self_upgrade.analyzer import UpgradeAnalyzer
from omnidesk_agent.self_upgrade.models import UpgradeRequest
from omnidesk_agent.self_upgrade.upgrader import SelfUpgrader


def main() -> None:
    parser = argparse.ArgumentParser(prog="omnidesk")
    parser.add_argument("--config", default="~/.omnidesk/config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor")
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

    report_p = sub.add_parser("upgrade-report", help="Generate a Level 1 self-upgrade report without changing code")
    report_p.add_argument("--output", default="UPGRADE_REPORT.md")
    report_p.add_argument("--limit", type=int, default=200)
    propose_p = sub.add_parser("propose-upgrade", help="Generate Level 2 upgrade proposal artifacts; does not apply source-code patches")
    propose_p.add_argument("request")
    propose_p.add_argument("--output-dir", default=".omnidesk/upgrades")
    propose_p.add_argument("--risk", choices=["low", "medium", "high", "critical"], default="medium")
    upgrade_p = sub.add_parser("upgrade", help="Prepare a Level 3 upgrade branch after explicit approval; never auto-merges or restarts")
    upgrade_p.add_argument("request")
    upgrade_p.add_argument("--approved", action="store_true", help="Required before creating an ai/* branch")
    upgrade_p.add_argument("--push", action="store_true", help="Push the ai/* branch after tests pass")
    upgrade_p.add_argument("--create-pr", action="store_true", help="Print a gh pr create command; does not merge")
    upgrade_p.add_argument("--risk", choices=["low", "medium", "high", "critical"], default="medium")

    args = parser.parse_args()
    cfg = load_config(args.config)

    if args.cmd == "doctor":
        rt = OmniDeskRuntime(cfg)
        print(json.dumps(rt.status(), ensure_ascii=False, indent=2))
        return

    if args.cmd == "run":
        rt = OmniDeskRuntime(cfg)
        msg = ChannelMessage(channel="local-cli", sender_id="owner", text=args.message)
        result = asyncio.run(rt.orchestrator.handle_message(msg))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.cmd == "remember":
        rt = OmniDeskRuntime(cfg)
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
        eid = rt.memory.add(args.text, tags=tags)
        print(f"remembered experience #{eid}")
        return

    if args.cmd == "search":
        rt = OmniDeskRuntime(cfg)
        print(json.dumps(rt.memory.search(args.query), ensure_ascii=False, indent=2))
        return


    if args.cmd == "upgrade-report":
        rt = OmniDeskRuntime(cfg)
        report = UpgradeAnalyzer(cfg.permissions.audit_log, rt.memory).build_report(limit=args.limit)
        from pathlib import Path
        out = Path(args.output)
        out.write_text(report, encoding="utf-8")
        print(f"wrote {out}")
        return

    if args.cmd == "propose-upgrade":
        req = UpgradeRequest(title=args.request, reason=args.request, risk=args.risk)
        run = asyncio.run(SelfUpgrader(Path.cwd()).propose(req, output_dir=args.output_dir))
        print(json.dumps({
            "status": run.status,
            "plan": run.plan.__dict__,
            "patch": run.patch.__dict__ if run.patch else None,
        }, ensure_ascii=False, indent=2))
        return

    if args.cmd == "upgrade":
        if not args.approved:
            raise SystemExit("Refusing to create an upgrade branch without --approved. Level 4 auto-merge/restart is not implemented.")
        from omnidesk_agent.tools.base import ToolContext
        branch = SelfUpgrader.new_upgrade_branch()
        rt = OmniDeskRuntime(cfg)
        ctx = ToolContext(permissions=rt.permissions, source="local-cli", actor="owner")
        req = UpgradeRequest(title=args.request, reason=args.request, risk=args.risk)
        up = SelfUpgrader(Path.cwd())
        run = asyncio.run(up.propose(req))
        run = asyncio.run(up.test_plan(run))
        if run.status == "failed":
            print(json.dumps({"status": run.status, "tests": [t.__dict__ for t in run.tests]}, ensure_ascii=False, indent=2))
            raise SystemExit(1)
        asyncio.run(rt.tools.call("git", "checkout_new_branch", {"branch": branch}, ctx))
        asyncio.run(rt.tools.call("git", "add", {}, ctx))
        asyncio.run(rt.tools.call("git", "commit", {"message": f"AI upgrade proposal: {args.request}"}, ctx))
        if args.push:
            asyncio.run(rt.tools.call("git", "push", {"branch": branch}, ctx))
        result = {"status": "committed", "branch": branch, "tests": [t.__dict__ for t in run.tests]}
        if args.create_pr:
            result["pull_request_command"] = f"gh pr create --title 'AI upgrade proposal: {args.request}' --body 'Level 3 proposal only. No auto-merge or auto-restart.' --base main --head {branch}"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.cmd == "serve":
        app = create_app(cfg)
        uvicorn.run(app, host=args.host or cfg.gateway.host, port=args.port or cfg.gateway.port)
        return
