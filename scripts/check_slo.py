#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omnidesk_agent.self_learning.observability.slo import IndustrialSLOEvaluator  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate runtime SLO metrics against OmniDesk industrial thresholds.")
    parser.add_argument("metrics_path", nargs="?", help="Path to a runtime metrics JSON file.")
    parser.add_argument("--metrics-file", dest="metrics_file", help="Path to a runtime metrics JSON file.")
    parser.add_argument("--json", action="store_true", help="Emit compact JSON.")
    parser.add_argument("--fail-on-error-budget", action="store_true", help="Exit non-zero when error or critical SLOs are violated.")
    args = parser.parse_args(argv)
    metrics_path = args.metrics_file or args.metrics_path
    if not metrics_path:
        parser.error("a metrics JSON file is required")
    metrics = json.loads(Path(metrics_path).read_text(encoding="utf-8"))
    result = IndustrialSLOEvaluator(IndustrialSLOEvaluator.runtime_targets()).evaluate(metrics)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, **({} if args.json else {"indent": 2})))
    return 1 if args.fail_on_error_budget and not result.get("ok") else 0


if __name__ == "__main__":
    raise SystemExit(main())
