"""Actual private CI runtime acceptance; never durable production or external GA."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import yaml
from cryptography.fernet import Fernet


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--serve", type=Path)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Cloud-only acceptance: requires GitHub Actions")
    if args.serve:
        import uvicorn
        from omnidesk_agent.config import load_config
        from omnidesk_agent.server import create_app

        uvicorn.run(create_app(load_config(args.serve)), host="127.0.0.1", port=18789,
                    ssl_certfile=os.environ["ACCEPTANCE_TLS_CERT"], ssl_keyfile=os.environ["ACCEPTANCE_TLS_KEY"])
        return

    args.output.mkdir(parents=True, exist_ok=True)
    report = {
        "kind": "ephemeral-cloud-acceptance", "customer_ga": False,
        "persistent_deployment": False, "native_publisher_signing": False,
        "run_id": os.environ["GITHUB_RUN_ID"],
        "checkout_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "checks": [], "ok": False,
    }
    processes = []
    logs = []
    sensitive = []
    started = time.monotonic()

    def check(label, condition):
        report["checks"].append({"name": label, "ok": bool(condition)})
        if not condition:
            raise RuntimeError(f"Acceptance check failed: {label}")

    def command(argv, **kwargs):
        return subprocess.run(argv, check=True, capture_output=True, **kwargs)

    with tempfile.TemporaryDirectory(prefix="omnidesk-private-ci-") as temporary:
        root = Path(temporary)
        env = dict(os.environ)
        for name in ("ADMIN_TOKEN", "VIEWER_TOKEN", "OPERATOR_TOKEN", "OWNER_TOKEN", "GATEWAY_SECRET",
                     "PLUGIN_SIGNING_SECRET", "SANDBOX_RUNNER_TOKEN", "SANDBOX_RUNNER_HMAC_SECRET",
                     "APPSYNC_SECRET_PEPPER", "AUDIT_CHECKPOINT_HMAC_KEY"):
            value = secrets.token_urlsafe(48)
            sensitive.append(value)
            env[f"OMNIDESK_{name}"] = value
            print(f"::add-mask::{value}", flush=True)
        key = Fernet.generate_key().decode()
        sensitive.append(key)
        env["OMNIDESK_MEMORY_ENCRYPTION_KEY"] = key
        print(f"::add-mask::{key}", flush=True)
        for role in ("ADMIN", "VIEWER", "OPERATOR", "OWNER"):
            env[f"OMNIDESK_{role}_ACTOR"] = "isolated-ci-actor"
        env.update(OMNIDESK_ENV="production", OMNIDESK_REQUIRE_PRODUCTION_GUARDS="true",
                   OMNIDESK_CONTAINER_RUNTIME="podman", OMNIDESK_SANDBOX_RUNNER_HOST="127.0.0.1",
                   OMNIDESK_SANDBOX_NONCE_DB=str(root / "nonces.sqlite3"),
                   OMNIDESK_BUILD_SHA=report["checkout_sha"],
                   ACCEPTANCE_TLS_CERT=str(root / "tls.crt"), ACCEPTANCE_TLS_KEY=str(root / "tls.key"))

        config = yaml.safe_load(Path("deploy/docker/config.production.example.yaml").read_text())

        def relocate(value):
            if isinstance(value, dict):
                return {k: relocate(v) for k, v in value.items()}
            if isinstance(value, list):
                return [relocate(v) for v in value]
            if isinstance(value, str) and value.startswith(("/data/", "/opt/")):
                return str(root / value.lstrip("/"))
            return value

        config = relocate(config)
        config["gateway"].update(host="127.0.0.1", public_base_url=None, admin_allowed_ips=["127.0.0.1"])
        config["sandbox"]["runner_url"] = "http://127.0.0.1:18890"
        config["app_sync"]["namespace"] = "isolated-ci"
        config["models"]["budget"].update(daily_usd_limit=0.01, monthly_usd_limit=0.01, per_actor_daily_usd_limit=0.01)
        config_path = root / "config.yaml"
        config_path.write_text(yaml.safe_dump(config))
        os.environ.update(env)
        tls = None

        def request(path, *, role=None, payload=None, headers=None):
            token_headers = {"Authorization": f"Bearer {env[f'OMNIDESK_{role.upper()}_TOKEN']}"} if role else {}
            token_headers.update(headers or {})
            body = json.dumps(payload).encode() if payload is not None else None
            if body is not None:
                token_headers.update({"Content-Type": "application/json", "Idempotency-Key": secrets.token_hex(16)})
            req = urllib.request.Request("https://127.0.0.1:18789" + path, data=body, headers=token_headers)
            try:
                with urllib.request.urlopen(req, timeout=15, context=tls) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())

        def launch(argv, name):
            path = root / f"{name}.log"
            stream = path.open("w")
            logs.append((path, stream))
            proc = subprocess.Popen(argv, env=env, stdout=stream, stderr=subprocess.STDOUT)
            processes.append(proc)
            return proc

        def start_app(name):
            proc = launch([sys.executable, __file__, "--output", str(args.output), "--serve", str(config_path)], name)
            for _ in range(90):
                if proc.poll() is not None:
                    raise RuntimeError(f"{name} terminated; see redacted process log")
                try:
                    if request("/health")[0] == 200:
                        return proc
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(1)
            raise RuntimeError(f"{name} startup timed out")

        def stop(proc):
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)

        try:
            command(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                     "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                     "-keyout", env["ACCEPTANCE_TLS_KEY"], "-out", env["ACCEPTANCE_TLS_CERT"]])
            tls = ssl.create_default_context(cafile=env["ACCEPTANCE_TLS_CERT"])
            check("TLS certificate verification enabled", tls.check_hostname and tls.verify_mode == ssl.CERT_REQUIRED)
            from omnidesk_agent.config import DEFAULT_SANDBOX_IMAGE, load_config
            from omnidesk_agent.validation.production import validate_production_config

            policy = validate_production_config(load_config(config_path))
            check("production guards pass without disabling policy", policy["production"] and policy["ok"])
            report["production_policy"] = policy
            info = json.loads(command(["podman", "info", "--format", "json"], text=True).stdout)
            check("sandbox runtime is rootless", info["host"]["security"]["rootless"])
            report["podman_version"] = command(["podman", "--version"], text=True).stdout.strip()
            command(["podman", "pull", DEFAULT_SANDBOX_IMAGE], text=True)
            report["sandbox_image"] = DEFAULT_SANDBOX_IMAGE
            launch([sys.executable, "-m", "omnidesk_agent.sandbox.runner_server"], "runner")
            proc = start_app("initial")
            check("unauthenticated admin rejected", request("/admin/status")[0] == 401)
            check("invalid token rejected", request("/admin/status", headers={"Authorization": "Bearer invalid"})[0] == 401)
            check("authenticated viewer accepted", request("/admin/status", role="viewer")[0] == 200)
            check("viewer cannot create conversation", request("/app/conversations", role="viewer", payload={"title": "denied"})[0] == 403)
            status, data = request("/app/conversations", role="operator", payload={"title": "Isolated actual HTTPS acceptance"})
            check("operator creates persisted conversation", status == 200 and data.get("ok"))
            conversation_id = data["conversation"]["conversation_id"]
            status, data = request(f"/app/conversations/{conversation_id}/messages", role="operator", payload={"content": "CI persistence marker", "risk": "low"})
            check("message write accepted", status == 200 and data.get("ok"))

            def verify_marker(label):
                status, data = request(f"/app/conversations/{conversation_id}/messages", role="viewer")
                check(label, status == 200 and "CI persistence marker" in json.dumps(data))

            verify_marker("message read after write")
            stop(proc)
            proc = start_app("restart")
            verify_marker("PostgreSQL state survives application restart")
            stop(proc)
            container = env["ACCEPTANCE_POSTGRES_CONTAINER"]
            dump = command(["docker", "exec", container, "pg_dump", "-U", "postgres", "--format=custom", "--no-owner", "--no-acl", "acceptance"]).stdout
            command(["docker", "exec", container, "createdb", "-U", "postgres", "acceptance_restore"])
            command(["docker", "exec", "-i", container, "pg_restore", "-U", "postgres", "--exit-on-error", "--no-owner", "--no-acl", "-d", "acceptance_restore"], input=dump)
            report["backup_sha256"] = hashlib.sha256(dump).hexdigest()
            report["backup_bytes"] = len(dump)
            env["OMNIDESK_POSTGRES_DSN"] = env["OMNIDESK_POSTGRES_DSN"].rsplit("/", 1)[0] + "/acceptance_restore"
            proc = start_app("restored")
            verify_marker("restored independent PostgreSQL database contains message")
            latencies = []
            for _ in range(30):
                before = time.monotonic()
                check("restored authenticated status request", request("/admin/status", role="viewer")[0] == 200)
                latencies.append(time.monotonic() - before)
                time.sleep(1)
            report["short_probe"] = {"requests": len(latencies), "seconds_minimum": 30,
                                     "max_latency_seconds": max(latencies), "long_soak": False}
            from production_smoke_test import check_sandbox

            os.environ["OMNIDESK_SMOKE_SANDBOX_URL"] = config["sandbox"]["runner_url"]
            report["sandbox"] = check_sandbox(strict=True)
            check("actual strict sandbox execution", report["sandbox"]["run"]["ok"])
            report["ok"] = True
        except Exception as error:
            report["error"] = str(error)
            raise
        finally:
            for proc in processes:
                if proc.poll() is None:
                    stop(proc)
            for path, stream in logs:
                stream.close()
                text = path.read_text()
                for value in sensitive:
                    text = text.replace(value, "[REDACTED]")
                (args.output / path.name).write_text(text)
            report["duration_seconds"] = time.monotonic() - started
            (args.output / "acceptance.json").write_text(json.dumps(report, indent=2) + "\n")
            hashes = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p in sorted(args.output.iterdir()) if p.is_file() and p.name != "SHA256SUMS"]
            (args.output / "SHA256SUMS").write_text("\n".join(hashes) + "\n")


if __name__ == "__main__":
    main()
