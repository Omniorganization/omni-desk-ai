"""Write a copy of a gateway config that the production validator refuses.

Used only by the self-healing failure-injection drill
(deploy/self-healing/drill-plan.bad-config.json): it turns on
gateway.allow_local_admin_without_token, which production refuses at boot, so
the gateway cannot start with it. The drill proves the watchdog restores the
last known-good config and restarts. Never point it at a live config path as
the destination.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    if args.destination.resolve() == args.source.resolve():
        print("refusing to overwrite the source config", file=sys.stderr)
        return 2
    config = yaml.safe_load(args.source.read_text(encoding="utf-8"))
    config.setdefault("gateway", {})["allow_local_admin_without_token"] = True
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    args.destination.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    print(f"wrote drill config {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
