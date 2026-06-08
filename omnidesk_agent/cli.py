from __future__ import annotations

import argparse
import asyncio
import json

import uvicorn

from omnidesk_agent.config import load_config
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.daemon import OmniDeskRuntime
from omnidesk_agent.server import create_app


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

    if args.cmd == "serve":
        app = create_app(cfg)
        uvicorn.run(app, host=args.host or cfg.gateway.host, port=args.port or cfg.gateway.port)
        return
