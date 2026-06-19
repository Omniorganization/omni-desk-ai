#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


METRIC_RE = re.compile(r"\bomnidesk_[a-zA-Z_:][a-zA-Z0-9_:]*\b")


def _extract_metrics(text: str) -> set[str]:
    return set(METRIC_RE.findall(text))


def _read_metrics(path: Path) -> set[str]:
    return _extract_metrics(path.read_text(encoding="utf-8"))


def _source_metric_refs(root: Path) -> set[str]:
    refs: set[str] = set()
    for base in [root / "omnidesk_agent", root / "scripts", root / "tests"]:
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            refs.update(_read_metrics(path))
    return refs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Prometheus, Grafana, and runtime metric naming contracts.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)

    root = Path(args.root)
    rules_path = root / "deploy" / "observability" / "prometheus-rules.yml"
    dashboard_path = root / "deploy" / "observability" / "grafana-dashboard.json"
    issues: list[str] = []

    if not rules_path.exists():
        issues.append(f"missing Prometheus rules: {rules_path}")
    if not dashboard_path.exists():
        issues.append(f"missing Grafana dashboard: {dashboard_path}")
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1

    rules_metrics = _read_metrics(rules_path)
    try:
        dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"invalid Grafana dashboard JSON: {exc}", file=sys.stderr)
        return 1
    dashboard_metrics = _extract_metrics(json.dumps(dashboard, ensure_ascii=False, sort_keys=True))
    source_refs = _source_metric_refs(root)

    for metric in sorted(dashboard_metrics - rules_metrics):
        issues.append(f"dashboard metric lacks Prometheus rule: {metric}")
    for metric in sorted(rules_metrics - dashboard_metrics):
        issues.append(f"Prometheus rule metric lacks dashboard panel: {metric}")
    for metric in sorted((rules_metrics | dashboard_metrics) - source_refs):
        issues.append(f"observability metric lacks code/test reference: {metric}")

    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1

    print(f"observability contract verified: {len(rules_metrics | dashboard_metrics)} metrics")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
