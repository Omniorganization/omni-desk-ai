#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


PINNED_NODE_ARG_RE = re.compile(r"^ARG\s+NODE_BASE_IMAGE=node:22-bookworm-slim@sha256:[0-9a-f]{64}$", re.MULTILINE)
FLOATING_NODE_FROM_RE = re.compile(r"^FROM\s+node:", re.MULTILINE)
FROM_BASE_RE = re.compile(r"^FROM\s+\$\{NODE_BASE_IMAGE\}\s+AS\s+(deps|build|runtime)$", re.MULTILINE)

REQUIRED_DOCKERFILE_SNIPPETS = {
    "standalone runtime copy": "COPY --from=build /app/.next/standalone ./",
    "static runtime copy": "COPY --from=build /app/.next/static ./.next/static",
    "public runtime copy": "COPY --from=build /app/public ./public",
    "non-root user": "USER 10001:10001",
    "tmp volume for read-only rootfs": 'VOLUME ["/tmp"]',
    "docker healthcheck": "HEALTHCHECK --interval=30s --timeout=5s --retries=3",
    "exec healthcheck": 'CMD ["node", "-e"',
    "standalone server command": 'CMD ["node", "server.js"]',
    "read-only rootfs label": 'omnidesk.runtime.read_only_rootfs="required"',
}

REQUIRED_DOCKERIGNORE = {
    "node_modules/",
    ".next/",
    ".npm-cache/",
    "coverage/",
    "*.tsbuildinfo",
    ".env",
    ".env.*",
}

REQUIRED_SECURITY_DOC = {
    "--read-only",
    "--tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m",
    "--cap-drop=ALL",
    "--security-opt no-new-privileges:true",
    "release_metadata.json",
}


def _read(path: Path, failures: list[str]) -> str:
    if not path.exists():
        failures.append(f"missing required file: {path}")
        return ""
    return path.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Web Admin container production hardening.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    failures: list[str] = []

    app_root = root / "apps" / "web-admin-next"
    dockerfile = _read(app_root / "Dockerfile", failures)
    dockerignore = set(_read(app_root / ".dockerignore", failures).splitlines())
    next_config = _read(app_root / "next.config.mjs", failures)
    security_doc = _read(app_root / "SECURITY_RELEASE.md", failures)

    if dockerfile:
        if not PINNED_NODE_ARG_RE.search(dockerfile):
            failures.append("Web Admin Dockerfile must pin NODE_BASE_IMAGE to node:22-bookworm-slim@sha256:<digest>")
        if FLOATING_NODE_FROM_RE.search(dockerfile):
            failures.append("Web Admin Dockerfile must not use floating FROM node:* references")
        stages = FROM_BASE_RE.findall(dockerfile)
        if stages != ["deps", "build", "runtime"]:
            failures.append("Web Admin Dockerfile must use NODE_BASE_IMAGE for deps, build, and runtime stages")
        for label, snippet in REQUIRED_DOCKERFILE_SNIPPETS.items():
            if snippet not in dockerfile:
                failures.append(f"Web Admin Dockerfile missing {label}: {snippet}")

    if dockerignore:
        missing = sorted(REQUIRED_DOCKERIGNORE - dockerignore)
        if missing:
            failures.append("Web Admin .dockerignore missing entries: " + ", ".join(missing))

    if next_config and "output: 'standalone'" not in next_config and 'output: "standalone"' not in next_config:
        failures.append("Web Admin Next config must enable standalone output for minimal runtime images")

    if security_doc:
        for snippet in sorted(REQUIRED_SECURITY_DOC):
            if snippet not in security_doc:
                failures.append(f"Web Admin security release doc missing runtime control: {snippet}")

    if failures:
        for failure in failures:
            print(f"BLOCKER {failure}", file=sys.stderr)
        return 1
    print("web admin container hardening ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
