"""Run the agent task-success suite: python -m omnidesk_agent.evals run [--mode scripted|live]."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

from omnidesk_agent.evals.agent_harness import DEFAULT_LIVE_KEY_ENV, ModelSpec, run_suite
from omnidesk_agent.evals.agent_scenarios import default_scenarios_dir, load_agent_scenarios


def _run_url() -> Optional[str]:
    server, repo, run = os.getenv("GITHUB_SERVER_URL"), os.getenv("GITHUB_REPOSITORY"), os.getenv("GITHUB_RUN_ID")
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omnidesk_agent.evals")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run the scenarios and write a JSON report")
    run.add_argument("--mode", choices=["scripted", "live"], default="scripted")
    run.add_argument("--scenarios", type=Path, default=default_scenarios_dir())
    run.add_argument("--only", action="append", default=[], help="scenario id to run (repeatable)")
    run.add_argument("--report", type=Path, help="where to write the JSON report")
    run.add_argument("--workdir", type=Path, help="scratch directory for the scenario workspaces")
    run.add_argument("--provider", default=os.getenv("OMNIDESK_EVAL_PROVIDER") or "openai")
    run.add_argument("--model", default=os.getenv("OMNIDESK_EVAL_MODEL") or "")
    run.add_argument("--base-url", default=os.getenv("OMNIDESK_EVAL_BASE_URL") or None)
    run.add_argument("--api-key-env", default=DEFAULT_LIVE_KEY_ENV)
    run.add_argument("--min-success-rate", type=float, default=None, help="exit 1 below this success rate")
    run.add_argument("--max-unsafe-effects", type=int, default=0, help="exit 1 above this many forbidden effects")
    run.add_argument("--max-safety-violations", type=int, default=None, help="exit 1 above this many scenarios with a violation")
    args = parser.parse_args(argv)

    if args.mode == "live":
        if not args.model:
            parser.error("--mode live needs --model (or OMNIDESK_EVAL_MODEL)")
        if args.api_key_env and not os.getenv(args.api_key_env) and args.provider != "ollama":
            parser.error(f"--mode live needs the model API key in ${args.api_key_env}")
    scenarios = load_agent_scenarios(args.scenarios)
    if args.only:
        unknown = sorted(set(args.only) - {s.id for s in scenarios})
        if unknown:
            parser.error(f"unknown scenario ids: {unknown}")
        scenarios = [s for s in scenarios if s.id in set(args.only)]
    spec = ModelSpec(mode=args.mode, provider=args.provider, model=args.model, base_url=args.base_url, api_key_env=args.api_key_env)
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="omnidesk-evals-"))

    report = asyncio.run(run_suite(scenarios, spec, workdir))
    data = {**report.to_dict(), "run_url": _run_url(), "commit": os.getenv("GITHUB_SHA")}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    metrics = data["metrics"]
    for result in data["results"]:
        mark = "PASS" if result["passed"] else "FAIL"
        detail = "; ".join(result["failures"] + result["unsafe_attempts"] + result["unsafe_effects"] + ([result["error"]] if result["error"] else []))
        print(f"{mark} {result['id']}: status={result['status']} steps={result['steps']} turns={result['turns']} llm_calls={result['llm_calls']} cost={result['cost_usd']} {detail}")
    print(json.dumps({"mode": data["mode"], "model": data["model"], **metrics}, ensure_ascii=False))

    failed = []
    if args.min_success_rate is not None and metrics["success_rate"] < args.min_success_rate:
        failed.append(f"success rate {metrics['success_rate']} < {args.min_success_rate}")
    if metrics["unsafe_effects"] > args.max_unsafe_effects:
        failed.append(f"{metrics['unsafe_effects']} unsafe effects > {args.max_unsafe_effects}")
    if args.max_safety_violations is not None and metrics["safety_violations"] > args.max_safety_violations:
        failed.append(f"{metrics['safety_violations']} scenarios with safety violations > {args.max_safety_violations}")
    for reason in failed:
        print(f"eval gate failed: {reason}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
