"""Render and validate the staging gateway config inside the release image.

Run by deploy/staging/bootstrap-host.sh as
  docker run --rm -i --env-file compose.env -e OMNIDESK_STAGING_BASE_CONFIG=<yaml> \
    <release image> python - [args] < render_staging_config.py
so the production validator of the exact release that will run decides whether
the configuration is acceptable. The base is deploy/docker/config.production.example.yaml
(passed as YAML text in OMNIDESK_STAGING_BASE_CONFIG). The rendered YAML goes to
stdout; if production policy refuses it, the validator's issues go to stderr
and the exit code is 2. Secret values are never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import yaml

from omnidesk_agent.config import AppConfig
from omnidesk_agent.validation.production import validate_production_config


def render(args: argparse.Namespace, base: dict) -> dict:
    config = base
    config["gateway"].update(
        host="0.0.0.0",  # nosec B104 - inside the container network; published on host loopback only
        public_base_url=f"https://{args.public_host}",
        admin_allowed_ips=[],
    )
    # Only Caddy (fixed address on the egress network) may set X-Forwarded-For.
    config["api_resource_guard"].update(trusted_proxy_ips=[args.trusted_proxy])
    config["sandbox"].update(runner_url="http://sandbox-runner:18890", docker_image=args.sandbox_image)
    config["app_sync"].update(namespace=args.namespace, postgres_dsn_env="OMNIDESK_APPSYNC_POSTGRES_DSN")
    config["storage"].update(backend="postgres", postgres_dsn_env="OMNIDESK_POSTGRES_DSN", require_multi_instance_safe=True)
    config.setdefault("observability", {})["otlp_endpoint_env"] = "OMNIDESK_OTLP_ENDPOINT"
    budget = config["models"].setdefault("budget", {})
    budget.update(daily_usd_limit=args.daily_usd_limit, monthly_usd_limit=args.monthly_usd_limit,
                  per_actor_daily_usd_limit=args.daily_usd_limit, on_exceed="block", require_persistent_ledger=True)
    if args.model_provider != "none":
        profile = {"provider": args.model_provider, "model": args.model, "api_key_env": "OMNIDESK_MODEL_API_KEY",
                   "max_output_tokens": 1200}
        config["models"]["profiles"] = {name: dict(profile) for name in ("fast", "planner", "code", "vision")}
    # Offline/local-model requirements of the example do not apply to this host.
    runtime = config.setdefault("runtime", {})
    runtime["required_ollama_models"] = []
    runtime["required_docker_images"] = [args.sandbox_image]
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-host", required=True)
    parser.add_argument("--namespace", default="staging")
    parser.add_argument("--trusted-proxy", default="172.30.0.10")
    parser.add_argument("--sandbox-image", required=True)
    parser.add_argument("--model-provider", default="none")
    parser.add_argument("--model", default="")
    parser.add_argument("--daily-usd-limit", type=float, default=2.0)
    parser.add_argument("--monthly-usd-limit", type=float, default=20.0)
    args = parser.parse_args(argv)
    base = yaml.safe_load(os.environ["OMNIDESK_STAGING_BASE_CONFIG"])
    config = render(args, base)
    policy = validate_production_config(AppConfig.model_validate(config), dict(os.environ))
    if not policy.get("ok"):
        print(json.dumps({"ok": False, "issues": policy.get("issues", [])}, indent=2), file=sys.stderr)
        return 2
    sys.stdout.write(yaml.safe_dump(config, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
