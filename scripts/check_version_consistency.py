#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def _read(path: Path) -> str:
    if not path.exists():
        raise RuntimeError(f"missing file: {path}")
    return path.read_text(encoding="utf-8")


def _regex(path: Path, pattern: str, label: str) -> str:
    text = _read(path)
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        raise RuntimeError(f"could not find {label} in {path}")
    return match.group(1)


def _all_regex(path: Path, pattern: str, label: str) -> list[str]:
    text = _read(path)
    values = re.findall(pattern, text, re.MULTILINE)
    if not values:
        raise RuntimeError(f"could not find {label} in {path}")
    return list(values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check OmniDesk package, workflow, Docker, and changelog version consistency.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    sources: dict[str, str] = {}
    sources["pyproject.toml"] = _regex(root / "pyproject.toml", r'^version\s*=\s*"([^"]+)"', "project version")
    sources["omnidesk_agent/__init__.py"] = _regex(root / "omnidesk_agent" / "__init__.py", r'^__version__\s*=\s*"([^"]+)"', "package __version__")

    docker_versions = _all_regex(root / "Dockerfile", r'^ARG\s+OMNIDESK_VERSION=([^\s]+)', "Dockerfile OMNIDESK_VERSION")
    for idx, value in enumerate(docker_versions, start=1):
        sources[f"Dockerfile ARG OMNIDESK_VERSION #{idx}"] = value

    sources[".github/workflows/release.yml expected version"] = _regex(
        root / ".github" / "workflows" / "release.yml",
        r'^\s*EXPECTED_VERSION:\s*([^\s]+)',
        "release.yml EXPECTED_VERSION",
    )

    for workflow in ("deploy-staging.yml", "promote-production.yml"):
        sources[f".github/workflows/{workflow} expected_version default"] = _regex(
            root / ".github" / "workflows" / workflow,
            r'expected_version:[\s\S]*?default:\s*([^\s]+)',
            f"{workflow} expected_version default",
        )

    sources["CHANGELOG.md latest heading"] = _regex(root / "CHANGELOG.md", r'^##\s+([^\s]+)', "latest changelog heading")

    unique = sorted(set(sources.values()))
    if len(unique) != 1:
        print("version consistency check failed:", file=sys.stderr)
        for label, value in sorted(sources.items()):
            print(f"  {label}: {value}", file=sys.stderr)
        return 1
    print(f"version consistency ok: {unique[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
