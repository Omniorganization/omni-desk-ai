#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

DIR_GATES = {
    "omnidesk_agent/security/": 90.0,
    "omnidesk_agent/core/": 85.0,
    "omnidesk_agent/tools/": 85.0,
}

FILE_GATES = {
    "omnidesk_agent/security/resource_guard.py": 95.0,
    "omnidesk_agent/security/admin_auth.py": 90.0,
    "omnidesk_agent/sandbox/runner_server.py": 85.0,
    "omnidesk_agent/sandbox/remote_runner.py": 85.0,
    "omnidesk_agent/models/schema_retry.py": 90.0,
    "omnidesk_agent/self_upgrade/sandbox_runner.py": 90.0,
    "omnidesk_agent/tools/shell.py": 90.0,
    "omnidesk_agent/oauth/gmail_oauth.py": 80.0,
}


def main(path: str = "coverage.json") -> int:
    report_path = Path(path)
    if not report_path.exists():
        print(f"coverage report not found: {report_path}", file=sys.stderr)
        return 2
    data = json.loads(report_path.read_text(encoding="utf-8"))
    files = {
        filename.replace("\\", "/").lstrip("./"): detail
        for filename, detail in data.get("files", {}).items()
    }
    failures: list[str] = []
    for prefix, threshold in DIR_GATES.items():
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
    for filename, threshold in FILE_GATES.items():
        detail = files.get(filename)
        if detail is None:
            failures.append(f"{filename} coverage report is missing")
            print(f"{filename}: missing required >= {threshold:.2f}%")
            continue
        summary = detail.get("summary", {})
        covered = int(summary.get("covered_lines", 0))
        statements = int(summary.get("num_statements", 0))
        pct = 100.0 if statements == 0 else covered * 100.0 / statements
        print(f"{filename}: {pct:.2f}% required >= {threshold:.2f}%")
        if pct + 1e-9 < threshold:
            failures.append(f"{filename} coverage {pct:.2f}% < {threshold:.2f}%")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*(sys.argv[1:] or [])))
