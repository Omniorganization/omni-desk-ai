from __future__ import annotations

import argparse
import secrets
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deploy/docker/config.production.yaml from the example template.")
    parser.add_argument("--public-base-url", required=True)
    parser.add_argument("--sandbox-image", required=True, help="Digest-pinned image such as python:3.11-slim@sha256:<64 hex>")
    parser.add_argument("--runner-url", default="http://sandbox-runner:18890")
    parser.add_argument("--output", default="deploy/docker/config.production.yaml")
    args = parser.parse_args()
    template = Path("deploy/docker/config.production.example.yaml").read_text(encoding="utf-8")
    text = template.replace("https://your-domain.example", args.public_base_url)
    text = text.replace("python:3.11-slim@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", args.sandbox_image)
    text = text.replace("http://sandbox-runner:18890", args.runner_url)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}")
    print("generate strong secrets separately; examples:")
    for name in ["OMNIDESK_ADMIN_TOKEN", "OMNIDESK_GATEWAY_SECRET", "OMNIDESK_PLUGIN_SIGNING_SECRET", "OMNIDESK_MEMORY_ENCRYPTION_KEY", "OMNIDESK_SANDBOX_RUNNER_TOKEN", "OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET"]:
        print(f"{name}={secrets.token_urlsafe(32)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
