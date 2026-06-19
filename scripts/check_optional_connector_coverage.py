#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

OPTIONAL_CONNECTORS = [
    "omnidesk_agent/channels/lark_feishu.py",
    "omnidesk_agent/channels/wechat_official.py",
    "omnidesk_agent/channels/dingtalk.py",
    "omnidesk_agent/channels/x_channel.py",
    "omnidesk_agent/channels/gmail.py",
    "omnidesk_agent/channels/ui_bridge.py",
]

OPTIONAL_CONNECTOR_MINIMUMS = {
    "omnidesk_agent/channels/lark_feishu.py": 35.0,
    "omnidesk_agent/channels/wechat_official.py": 40.0,
    "omnidesk_agent/channels/dingtalk.py": 45.0,
    "omnidesk_agent/channels/x_channel.py": 50.0,
    "omnidesk_agent/channels/gmail.py": 40.0,
    "omnidesk_agent/channels/ui_bridge.py": 80.0,
}

PRODUCTION_CRITICAL = [
    "omnidesk_agent/server_routes/webhook_guard.py",
    "omnidesk_agent/self_learning/runtime_loop.py",
    "omnidesk_agent/tools/browser.py",
    "omnidesk_agent/server.py",
]

PRODUCTION_CRITICAL_MINIMUMS = {
    "omnidesk_agent/server_routes/webhook_guard.py": 55.0,
    "omnidesk_agent/self_learning/runtime_loop.py": 50.0,
    "omnidesk_agent/tools/browser.py": 70.0,
    "omnidesk_agent/server.py": 75.0,
}


def _pct(detail: dict) -> float:
    summary = detail.get("summary", {})
    statements = int(summary.get("num_statements", 0))
    covered = int(summary.get("covered_lines", 0))
    return 100.0 if statements == 0 else covered * 100.0 / statements


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gate optional connector and production-critical coverage baselines.")
    parser.add_argument("coverage_json", nargs="?", default="coverage.json")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    path = Path(args.coverage_json)
    if not path.exists():
        print(f"coverage report not found: {path}")
        return 2
    files = {name.replace("\\", "/").lstrip("./"): detail for name, detail in json.loads(path.read_text(encoding="utf-8")).get("files", {}).items()}
    result: dict[str, dict[str, float] | list[dict[str, float | str]]] = {
        "optional_connectors": {name: round(_pct(files[name]), 2) for name in OPTIONAL_CONNECTORS if name in files},
        "production_critical": {name: round(_pct(files[name]), 2) for name in PRODUCTION_CRITICAL if name in files},
        "failures": [],
    }
    failures: list[dict[str, float | str]] = []
    for group, minimums in (
        ("optional_connectors", OPTIONAL_CONNECTOR_MINIMUMS),
        ("production_critical", PRODUCTION_CRITICAL_MINIMUMS),
    ):
        values = result[group]
        assert isinstance(values, dict)
        for name, minimum in minimums.items():
            if name not in values:
                failures.append({"group": group, "file": name, "coverage": 0.0, "required": minimum, "reason": "missing"})
                continue
            coverage = float(values[name])
            if coverage < minimum:
                failures.append({"group": group, "file": name, "coverage": coverage, "required": minimum, "reason": "below_threshold"})
    result["failures"] = failures
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for group in ("optional_connectors", "production_critical"):
            values = result[group]
            assert isinstance(values, dict)
            print(group)
            for name, pct in values.items():
                print(f"  {name}: {pct:.2f}%")
        if failures:
            print("coverage baseline failures")
            for failure in failures:
                print(f"  {failure['group']} {failure['file']}: {failure['coverage']:.2f}% required >= {failure['required']:.2f}% ({failure['reason']})")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
