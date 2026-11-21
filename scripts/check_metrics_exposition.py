"""Write the gateway's real /admin/metrics exposition after some traffic.

Boots the FastAPI gateway in-process with an isolated workspace, sends a few
requests (including a 404 and the probes), traces a span, then fetches
/admin/metrics with an admin token and writes the body to --output so CI can
run ``promtool check metrics`` on exactly what Prometheus would scrape.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    from fastapi.testclient import TestClient

    from omnidesk_agent.config import AppConfig
    from omnidesk_agent.server import create_app

    token = "exposition-check-" + os.urandom(16).hex()
    os.environ["OMNIDESK_ADMIN_TOKEN"] = token
    os.environ.setdefault("OMNIDESK_GATEWAY_SECRET", os.urandom(24).hex())
    with tempfile.TemporaryDirectory(prefix="omnidesk-exposition-") as tmp:
        root = Path(tmp)
        cfg = AppConfig()
        cfg.workspace.root = root
        cfg.workspace.memory_db = root / "memory.sqlite3"
        cfg.workspace.skills_dirs = [root / "skills"]
        cfg.workspace.plugins_dirs = [root / "plugins"]
        cfg.permissions.audit_log = root / "audit.log"
        cfg.channels.gmail.credentials_file = root / "google" / "credentials.json"
        cfg.channels.gmail.token_file = root / "google" / "token.json"
        cfg.learning.growth_plan_file = root / "growth_plan.json"
        cfg.gateway.allow_local_admin_without_token = False
        cfg.gateway.admin_allowed_ips = ["127.0.0.1", "testclient"]
        cfg.plugins.enabled = False
        cfg.ensure_dirs()
        app = create_app(cfg)
        with TestClient(app) as client:
            for path in ("/health", "/ready", "/does-not-exist"):
                client.get(path, headers={"x-request-id": "exposition-check"})
            response = client.get("/admin/metrics", headers={"authorization": f"Bearer {token}"})
            if response.status_code != 200:
                print(f"/admin/metrics returned HTTP {response.status_code}", file=sys.stderr)
                return 1
            body = response.text
    args.output.write_text(body, encoding="utf-8")
    families = sum(1 for line in body.splitlines() if line.startswith("# TYPE "))
    print(f"wrote {args.output}: {len(body.splitlines())} lines, {families} metric families")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
