#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

SHA_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[a-f0-9]{40}$")
USES_RE = re.compile(r"^\s*uses:\s*([^\s#]+)")


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(".github/workflows")
    failures: list[str] = []
    for wf in sorted(root.glob("*.yml")) + sorted(root.glob("*.yaml")):
        for lineno, line in enumerate(wf.read_text(encoding="utf-8").splitlines(), start=1):
            match = USES_RE.match(line)
            if not match:
                continue
            target = match.group(1).strip().strip('"\'')
            if target.startswith("./"):
                continue
            if not SHA_RE.match(target):
                failures.append(f"{wf}:{lineno}: action must be pinned to a full 40-char commit SHA: {target}")
    if failures:
        for item in failures:
            print(item, file=sys.stderr)
        return 1
    print(f"github actions pinning ok: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
