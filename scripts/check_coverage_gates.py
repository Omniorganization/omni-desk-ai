#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

GATES = {
    "omnidesk_agent/security/": 90.0,
    "omnidesk_agent/core/": 85.0,
    "omnidesk_agent/tools/": 85.0,
}


def main(path: str = "coverage.json") -> int:
    report_path = Path(path)
    if not report_path.exists():
        print(f"coverage report not found: {report_path}", file=sys.stderr)
        return 2
    data = json.loads(report_path.read_text(encoding="utf-8"))
    files = data.get("files", {})
    failures: list[str] = []
    for prefix, threshold in GATES.items():
        covered = 0
        statements = 0
        for filename, detail in files.items():
            normalized = filename.replace("\\", "/")
            if not normalized.startswith(prefix):
                continue
            summary = detail.get("summary", {})
            covered += int(summary.get("covered_lines", 0))
            statements += int(summary.get("num_statements", 0))
        pct = 100.0 if statements == 0 else covered * 100.0 / statements
        print(f"{prefix}: {pct:.2f}% required >= {threshold:.2f}%")
        if pct + 1e-9 < threshold:
            failures.append(f"{prefix} coverage {pct:.2f}% < {threshold:.2f}%")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*(sys.argv[1:] or [])))
