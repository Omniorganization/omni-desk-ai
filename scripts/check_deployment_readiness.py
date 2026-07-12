#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REQUIRED_ENV = [
    "OMNIDESK_ADMIN_TOKEN",
    "OMNIDESK_GATEWAY_SECRET",
    "OMNIDESK_MEMORY_ENCRYPTION_KEY",
    "OMNIDESK_SANDBOX_RUNNER_TOKEN",
    "OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET",
    "OMNIDESK_SANDBOX_IMAGE_ALLOWLIST",
    "OMNIDESK_POSTGRES_DSN",
]
DIGEST_RE = re.compile(r"@sha256:[a-f0-9]{64}")
PRODUCTION_CONFIG_PATH = "/data/config.production.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail closed when production deployment prerequisites are incomplete.")
    parser.add_argument("--compose-file", default="deploy/docker/docker-compose.full.yml")
    parser.add_argument("--check-env", action="store_true")
    args = parser.parse_args(argv)

    compose = Path(args.compose_file)
    issues: list[str] = []
    for required_script in ["scripts/backup_postgres.py", "scripts/restore_postgres.py"]:
        if not Path(required_script).exists():
            issues.append(f"missing PostgreSQL operations script: {required_script}")

    if not compose.exists():
        issues.append(f"compose file missing: {compose}")
    else:
        text = compose.read_text(encoding="utf-8")
        if "/var/run/docker.sock" in text:
            issues.append("app deployment must not mount /var/run/docker.sock")
        for required in [
            "OMNIDESK_ENV: production",
            f"OMNIDESK_CONFIG: {PRODUCTION_CONFIG_PATH}",
            "OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET",
            "OMNIDESK_SANDBOX_REQUIRE_HMAC",
            "OMNIDESK_SANDBOX_IMAGE_ALLOWLIST",
            "OMNIDESK_SANDBOX_NONCE_DB",
            "DOCKER_HOST",
            "OMNIDESK_CONTAINER_RUNTIME",
            "OMNIDESK_CONTAINER_SOCKET_PATH",
            "postgres:",
            "OMNIDESK_POSTGRES_DSN",
            "omnidesk-postgres",
            "pg_isready",
            "omnidesk-postgres:",
        ]:
            if required not in text:
                issues.append(f"compose file missing {required}")
        if "OMNIDESK_CONFIG: /data/config.yaml" in text:
            issues.append("production compose must not point OMNIDESK_CONFIG at the dev config path")
        if "OMNIDESK_SANDBOX_ALLOWED_IMAGE" in text:
            issues.append("legacy OMNIDESK_SANDBOX_ALLOWED_IMAGE must not be used; use OMNIDESK_SANDBOX_IMAGE_ALLOWLIST")
        if "/ready" not in text:
            issues.append("compose healthchecks must use /ready, not shallow /health")
        if "postgres_password" not in text:
            issues.append("compose file must use a postgres password secret")

    if args.check_env:
        for name in REQUIRED_ENV:
            value = os.getenv(name, "")
            if len(value) < 32 and name != "OMNIDESK_SANDBOX_IMAGE_ALLOWLIST":
                issues.append(f"{name} must be set to a strong value")
            if name == "OMNIDESK_SANDBOX_IMAGE_ALLOWLIST" and not DIGEST_RE.search(value):
                issues.append("OMNIDESK_SANDBOX_IMAGE_ALLOWLIST must contain at least one digest-pinned image")
            if name == "OMNIDESK_POSTGRES_DSN" and not value.startswith(("postgresql://", "postgres://")):
                issues.append("OMNIDESK_POSTGRES_DSN must be a PostgreSQL DSN")

    dockerfile = Path("Dockerfile")
    if dockerfile.exists():
        dtext = dockerfile.read_text(encoding="utf-8")
        if "requirements.enterprise.lock" not in dtext:
            issues.append("Dockerfile must install PostgreSQL enterprise dependencies from requirements.enterprise.lock")
        if "psycopg[binary]>=" in dtext or "pip install --no-cache-dir \"psycopg" in dtext:
            issues.append("Dockerfile must not install PostgreSQL dependencies outside hash-locked lockfiles")
        if "/health" not in dtext:
            issues.append("Dockerfile liveness HEALTHCHECK must use /health")
        if f"OMNIDESK_CONFIG={PRODUCTION_CONFIG_PATH}" not in dtext:
            issues.append("Dockerfile must default OMNIDESK_CONFIG to the production config path")
        if f"--config\", \"{PRODUCTION_CONFIG_PATH}\"" not in dtext:
            issues.append("Dockerfile CMD must serve with the production config path")
        if "/data/config.yaml" in dtext:
            issues.append("Dockerfile must not use the ambiguous dev config path in production runtime")

    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("deployment readiness ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
