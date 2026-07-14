#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


REQUIRED_SNIPPETS = {
    "requirements.enterprise.lock": (
        "psycopg==",
        "psycopg-binary==",
        "--hash=sha256:",
    ),
    "Dockerfile": (
        "requirements.enterprise.lock",
        "pip install --no-cache-dir --require-hashes -r /tmp/requirements.enterprise.lock",
    ),
}


def _read(root: Path, rel: str) -> str:
    path = root / rel
    if not path.exists():
        raise FileNotFoundError(rel)
    return path.read_text(encoding="utf-8")


def audit(root: Path) -> list[str]:
    failures: list[str] = []
    for rel, snippets in REQUIRED_SNIPPETS.items():
        try:
            text = _read(root, rel)
        except FileNotFoundError:
            failures.append(f"missing required file: {rel}")
            continue
        for snippet in snippets:
            if snippet not in text:
                failures.append(f"{rel} missing required snippet: {snippet}")

    try:
        pyproject = _read(root, "pyproject.toml")
    except FileNotFoundError:
        failures.append("missing required file: pyproject.toml")
        pyproject = ""
    if pyproject and not re.search(
        r'enterprise\s*=\s*\[[^\]]*"psycopg\[binary\]>=3\.2,<4"',
        pyproject,
        re.DOTALL,
    ):
        failures.append(
            "pyproject.toml enterprise extra must bound psycopg to >=3.2,<4"
        )

    lock_path = root / "requirements.enterprise.lock"
    lock_text = _read(root, "requirements.enterprise.lock") if lock_path.exists() else ""
    if lock_text and not re.search(
        r"^psycopg==\d+\.\d+\.\d+", lock_text, re.MULTILINE
    ):
        failures.append("requirements.enterprise.lock must pin psycopg")
    if lock_text and not re.search(
        r"^psycopg-binary==\d+\.\d+\.\d+", lock_text, re.MULTILINE
    ):
        failures.append("requirements.enterprise.lock must pin psycopg-binary")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate enterprise dependency lock and production image wiring."
        )
    )
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    failures = audit(root)
    if failures:
        print("enterprise dependency contract failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("enterprise dependency contract ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
