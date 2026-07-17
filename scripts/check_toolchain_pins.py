#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def main(root: str = ".") -> int:
    base = Path(root)
    contract = json.loads(
        (base / ".github/toolchains.json").read_text(encoding="utf-8")
    )
    expected = {
        "node-version": str(contract["node"]),
        "flutter-version": str(contract["flutter"]),
        "toolchain": str(contract["rust"]),
    }
    issues: list[str] = []
    for path in sorted((base / ".github/workflows").glob("*.yml")):
        if path.name.startswith("_"):
            continue
        text = path.read_text(encoding="utf-8")
        if "actions/setup-node@" in text:
            for value in re.findall(
                r"(?m)^\s*node-version:\s*[\"']?([^\"'\s]+)", text
            ):
                if value != expected["node-version"]:
                    issues.append(
                        f"{path}: node-version {value} != {expected['node-version']}"
                    )
        if "subosito/flutter-action@" in text:
            for value in re.findall(
                r"(?m)^\s*flutter-version:\s*[\"']?([^\"'\s]+)", text
            ):
                if value != expected["flutter-version"]:
                    issues.append(
                        f"{path}: flutter-version {value} != {expected['flutter-version']}"
                    )
        if "dtolnay/rust-toolchain@" in text:
            for value in re.findall(
                r"(?m)^\s*toolchain:\s*[\"']?([^\"'\s]+)", text
            ):
                if value != expected["toolchain"]:
                    issues.append(
                        f"{path}: Rust toolchain {value} != {expected['toolchain']}"
                    )
    if issues:
        print("\n".join(issues), file=sys.stderr)
        return 1
    print(
        "toolchain pins verified: "
        f"node={contract['node']} rust={contract['rust']} "
        f"flutter={contract['flutter']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*(sys.argv[1:] or [])))
