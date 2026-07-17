#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "release/evidence-policy-v1.json"
AUDIT = ROOT / "release/real-ga-evidence-audit-1.12.7.json"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    base = policy.get("base_categories") or {}
    extended = policy.get("extended_categories") or {}
    external = load_module(ROOT / "scripts/check_external_ga_evidence.py", "external_ga_policy")
    if set(external.REQUIRED_EVIDENCE) != set(base):
        raise SystemExit("check_external_ga_evidence.py category set differs from evidence-policy-v1.json")
    expected = set(base) | set(extended)
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    actual = set((audit.get("categories") or {}).keys())
    if actual != expected:
        raise SystemExit(f"static Real GA audit categories differ: expected={sorted(expected)} actual={sorted(actual)}")
    blockers = sum(1 for item in audit["categories"].values() if not item.get("ok"))
    if int(audit.get("blocker_count", -1)) != blockers:
        raise SystemExit("static Real GA audit blocker_count is stale")
    print(f"Real GA evidence policy verified across {len(expected)} categories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
