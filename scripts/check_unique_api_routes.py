#!/usr/bin/env python3
from __future__ import annotations

from collections import defaultdict

from omnidesk_agent.config import AppConfig
from omnidesk_agent.server import create_app


def main() -> int:
    app = create_app(AppConfig())
    owners: dict[tuple[str, str], list[str]] = defaultdict(list)
    for route in app.routes:
        path = str(getattr(route, "path", ""))
        methods = set(getattr(route, "methods", set()) or set()) - {"HEAD", "OPTIONS"}
        for method in methods:
            owners[(method, path)].append(str(getattr(route, "name", "unnamed")))
    duplicates = {key: names for key, names in owners.items() if len(names) > 1}
    if duplicates:
        for (method, path), names in sorted(duplicates.items()):
            print(f"duplicate API route {method} {path}: {names}")
        return 1
    print(f"verified {len(owners)} unique API method/path pairs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
