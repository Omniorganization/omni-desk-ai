from __future__ import annotations

import sys
from pathlib import Path

import yaml


def main(argv: list[str] | None = None) -> int:
    root = Path(argv[0]) if argv else Path(".github/workflows")
    if not root.exists():
        print(f"workflow directory not found: {root}", file=sys.stderr)
        return 2
    ok = True
    for path in sorted(root.glob("*.yml")) + sorted(root.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - failure path is CLI-focused
            ok = False
            print(f"{path}: invalid YAML: {exc}", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            ok = False
            print(f"{path}: workflow root must be a mapping", file=sys.stderr)
            continue
        # PyYAML YAML 1.1 parses the GitHub Actions key `on` as boolean True.
        # Accept that parse quirk, but still require the effective trigger key.
        if "on" not in data and True not in data:
            ok = False
            print(f"{path}: missing GitHub Actions 'on' trigger", file=sys.stderr)
        if "jobs" not in data:
            ok = False
            print(f"{path}: missing jobs section", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
