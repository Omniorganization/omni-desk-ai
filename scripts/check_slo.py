#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

from omnidesk_agent.self_learning.observability.slo import IndustrialSLOEvaluator


def main(path: str) -> int:
    metrics = json.loads(Path(path).read_text(encoding="utf-8"))
    result = IndustrialSLOEvaluator(IndustrialSLOEvaluator.runtime_targets()).evaluate(metrics)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: scripts/check_slo.py runtime_metrics.json", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
