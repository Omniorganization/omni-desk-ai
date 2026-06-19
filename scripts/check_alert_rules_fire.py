#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ALERT_RE = re.compile(r"^\s*-\s*alert:\s*([A-Za-z0-9_:.-]+)", re.MULTILINE)
EXPR_RE = re.compile(r"^\s*expr:\s*(.+)$", re.MULTILINE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Static alert-rule drill: verify each alert has an expression and known OmniDesk metric reference.")
    parser.add_argument("rules", nargs="?", default="deploy/observability/prometheus-rules.yml", type=Path)
    args = parser.parse_args(argv)
    text = args.rules.read_text(encoding="utf-8")
    alerts = ALERT_RE.findall(text)
    exprs = EXPR_RE.findall(text)
    metric_refs = sorted(set(re.findall(r"(omnidesk_[a-zA-Z0-9_]+)", text)))
    failures: list[str] = []
    if not alerts:
        failures.append("no alerts found")
    if len(exprs) < len(alerts):
        failures.append(f"alert expression count mismatch: alerts={len(alerts)} exprs={len(exprs)}")
    if not metric_refs:
        failures.append("no omnidesk_* metrics referenced")
    payload = {"ok": not failures, "alerts": alerts, "metric_refs": metric_refs, "failures": failures}
    print(json.dumps(payload, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
