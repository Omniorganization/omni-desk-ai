from __future__ import annotations

import argparse
import os
from pathlib import Path

from omnidesk_agent.config import load_config
from omnidesk_agent.validation.production import validate_production_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deploy/docker/config.production.yaml from the example template.")
    parser.add_argument("--public-base-url", required=True)
    parser.add_argument("--sandbox-image", required=True, help="Digest-pinned image such as python:3.11-slim@sha256:<64 hex>")
    parser.add_argument("--runner-url", default="http://sandbox-runner:18890")
    parser.add_argument("--output", default="deploy/docker/config.production.yaml")
    parser.add_argument("--validate", action="store_true", help="Validate the generated config against the current environment.")
    args = parser.parse_args()
    template = Path("deploy/docker/config.production.example.yaml").read_text(encoding="utf-8")
    text = template.replace("https://omnidesk.company.example.invalid", args.public_base_url)
    text = text.replace("python:3.11-slim@sha256:f9fa7f851e38bfb19c9de3afbc4b86ae7176ea7aaf94535c31df5458d5849457", args.sandbox_image)
    text = text.replace("http://sandbox-runner:18890", args.runner_url)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}")
    print("configure these required secrets in your secret manager; do not print generated values to logs:")
    for name in [
        "OMNIDESK_ADMIN_TOKEN",
        "OMNIDESK_GATEWAY_SECRET",
        "OMNIDESK_PLUGIN_SIGNING_SECRET",
        "OMNIDESK_MEMORY_ENCRYPTION_KEY",
        "OMNIDESK_SANDBOX_RUNNER_TOKEN",
        "OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET",
        "OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY",
        "OMNIDESK_POSTGRES_DSN",
    ]:
        print(f"- {name}")
    if args.validate:
        env = dict(os.environ)
        env.setdefault("OMNIDESK_ENV", "production")
        result = validate_production_config(load_config(out), env)
        if not result["ok"]:
            issue_count = len(result["issues"])
            print(f"validation failed: {issue_count} issue(s); details withheld to avoid logging sensitive config")
            return 2
        print("production config validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
