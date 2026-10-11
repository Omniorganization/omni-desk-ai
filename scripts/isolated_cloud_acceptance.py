"""Actual private CI runtime acceptance; never durable production or external GA."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
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
    parser.add_argument("--web-first", action="store_true", help="Also exercise the production Web image in a real browser")
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

    def deadline(_signum, _frame):
        raise TimeoutError("Cloud acceptance exceeded its ten-minute execution deadline")

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(600)

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
        print(f"Acceptance: {label}: {bool(condition)}", flush=True)
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
                   OMNIDESK_SANDBOX_READY_SMOKE="1",
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
        if args.web_first:
            # The acceptance model is real, local to the ephemeral cloud runner, and free.
            # It does not establish a persistent production model provider.
            profile = {"provider": "ollama", "model": "smollm2:135m", "api_key_env": None,
                       "base_url": "http://127.0.0.1:11434", "max_output_tokens": 64}
            config["models"]["profiles"] = {name: dict(profile) for name in ("fast", "planner", "local")}
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
            report["cgroup"] = {"version": info["host"]["cgroupVersion"], "manager": info["host"]["cgroupManager"]}
            check("rootless resource controls use cgroup v2", report["cgroup"]["version"] == "v2")
            report["podman_version"] = command(["podman", "--version"], text=True).stdout.strip()
            command(["podman", "pull", DEFAULT_SANDBOX_IMAGE], text=True)
            report["sandbox_image"] = DEFAULT_SANDBOX_IMAGE
            command([sys.executable, "-m", "omnidesk_agent.appsync.migrate", "--dsn-env", "OMNIDESK_POSTGRES_DSN",
                     "--namespace", config["app_sync"]["namespace"]], env=env, text=True)
            migration = command([sys.executable, "-m", "omnidesk_agent.appsync.migrate", "--dsn-env", "OMNIDESK_POSTGRES_DSN",
                                 "--namespace", config["app_sync"]["namespace"], "--check"], env=env, text=True)
            report["migration"] = json.loads(migration.stdout)
            check("explicit schema migration is current", report["migration"]["ready"])
            launch([sys.executable, "-m", "omnidesk_agent.sandbox.runner_server"], "runner")
            proc = start_app("initial")
            try:
                with urllib.request.urlopen("https://127.0.0.1:18789/health", timeout=5, context=ssl.create_default_context()):
                    trusted_without_ca = True
            except urllib.error.URLError as error:
                trusted_without_ca = not isinstance(error.reason, ssl.SSLCertVerificationError)
            check("private certificate rejected without explicit test CA trust", not trusted_without_ca)
            status, denied = request("/admin/status")
            report["unauthenticated_admin"] = {"status": status, "detail": denied.get("detail")}
            check("unauthenticated admin rejected", status == 403 and denied.get("detail") == "missing or invalid admin token")
            status, denied = request("/admin/status", headers={"Authorization": "Bearer invalid"})
            report["invalid_admin_token"] = {"status": status, "detail": denied.get("detail")}
            check("invalid token rejected", status == 403 and denied.get("detail") == "missing or invalid admin token")
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
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            private_key = Ed25519PrivateKey.generate()
            public_key = private_key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            ).decode()
            device_id = secrets.token_hex(20)
            status, data = request("/app/devices/register", role="operator", payload={
                "device_id": device_id, "device_type": "desktop", "platform": "isolated-ci",
                "public_key": public_key,
            })
            check("synthetic asymmetric device registered", status == 200 and data.get("ok"))
            pairing = secrets.token_urlsafe(24)
            sensitive.append(pairing)
            status, data = request("/app/devices/enrollment/start", role="owner", payload={
                "device_type": "desktop", "pairing_code": pairing,
            })
            check("owner initiates device enrollment", status == 200 and data.get("ok"))
            enrollment = "/app/devices/enrollment/" + data["enrollment"]["enrollment_id"]
            status, data = request(enrollment + "/complete", role="operator", payload={
                "pairing_code": pairing, "device_id": device_id, "public_key": public_key,
            })
            check("device enrollment completion", status == 200 and data.get("ok"))
            status, data = request(enrollment + "/challenge", role="operator", payload={"device_id": device_id})
            check("device challenge issued", status == 200 and data.get("ok"))
            challenge = data["challenge"]
            verification = {
                "device_id": device_id, "challenge_id": challenge["challenge_id"],
                "signature": private_key.sign(challenge["signing_message"].encode()).hex(),
            }
            status, data = request(enrollment + "/verify", role="operator", payload=verification)
            check("real Ed25519 challenge signature verified", status == 200 and data.get("ok"))
            sensitive.append(data["credential"]["device_token"])
            check("used challenge rejected", request(enrollment + "/verify", role="operator", payload=verification)[0] == 409)
            rotation_path = f"/app/devices/{device_id}/rotate-token"
            check("unsigned device request rejected", request(rotation_path, role="operator", payload={})[0] == 401)
            timestamp = str(int(time.time() * 1000))
            nonce = secrets.token_hex(24)
            body_hash = hashlib.sha256(b"{}").hexdigest()
            # Construct the documented protocol independently of the server helper.
            message = f"omnidesk-device-request:v1:POST:{rotation_path}:{body_hash}:{timestamp}:{nonce}".encode()
            signed = {
                "x-omnidesk-device-id": device_id, "x-omnidesk-timestamp": timestamp,
                "x-omnidesk-nonce": nonce, "x-omnidesk-device-signature": private_key.sign(message).hex(),
            }
            status, data = request(rotation_path, role="operator", payload={}, headers=signed)
            check("signed device request accepted", status == 200 and data.get("ok"))
            sensitive.append(data["device"]["device_token"])
            check("device request nonce replay rejected", request(rotation_path, role="operator", payload={}, headers=signed)[0] == 401)
            stop(proc)
            proc = start_app("restart")
            verify_marker("PostgreSQL state survives application restart")
            check("device nonce replay rejected after restart", request(rotation_path, role="operator", payload={}, headers=signed)[0] == 401)
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
            check("device nonce replay rejected after database restore", request(rotation_path, role="operator", payload={}, headers=signed)[0] == 401)
            latencies = []
            for _ in range(30):
                before = time.monotonic()
                check("restored authenticated status request", request("/admin/status", role="viewer")[0] == 200)
                latencies.append(time.monotonic() - before)
                time.sleep(1)
            report["short_probe"] = {"requests": len(latencies), "seconds_minimum": 30,
                                     "max_latency_seconds": max(latencies), "long_soak": False}
            from production_smoke_test import _file_archive_base64, check_sandbox, post_json

            os.environ["OMNIDESK_SMOKE_SANDBOX_URL"] = config["sandbox"]["runner_url"]
            probe = b'''import errno, json, os
from pathlib import Path
status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
def readonly(path):
    try:
        Path(path).write_text("must not persist")
    except OSError as error:
        return error.errno == errno.EROFS
    return False
quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
observed = {
    "uid": os.geteuid(), "gid": os.getegid(), "capabilities_zero": int(status["CapEff"].strip(), 16) == 0,
    "no_new_privileges": status["NoNewPrivs"].strip() == "1", "seccomp_filter": status["Seccomp"].strip() == "2",
    "workspace_readonly": readonly("/workspace/mutation"), "rootfs_readonly": readonly("/mutation"),
    "memory_bytes": int(Path("/sys/fs/cgroup/memory.max").read_text()),
    "pids_limit": int(Path("/sys/fs/cgroup/pids.max").read_text()), "cpu_cores": int(quota) / int(period),
}
Path("/tmp/scratch").write_text("bounded scratch")
observed["scratch_writable"] = Path("/tmp/scratch").read_text() == "bounded scratch"
print(json.dumps(observed, sort_keys=True))
'''
            observed = post_json(config["sandbox"]["runner_url"] + "/v1/run", {
                "argv": ["python3", "-I", "probe.py"], "purpose": "plugin", "readonly": True,
                "workspace_archive_base64": _file_archive_base64("probe.py", probe), "timeout_seconds": 30,
            }, env["OMNIDESK_SANDBOX_RUNNER_TOKEN"], env["OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET"])
            report["sandbox_probe_raw"] = observed
            check("actual sandbox isolation probe completes", observed.get("ok"))
            observation = json.loads(observed["stdout"])
            report["sandbox_isolation"] = observation
            check("container has fixed unprivileged identity", observation["uid"] == 65534 and observation["gid"] == 65534)
            check("actual sandbox caps, seccomp and privilege restrictions", all(observation[k] for k in (
                "capabilities_zero", "no_new_privileges", "seccomp_filter")))
            check("actual workspace/rootfs read-only and scratch writable", all(observation[k] for k in (
                "workspace_readonly", "rootfs_readonly", "scratch_writable")))
            check("actual resource limits enforced", observation["memory_bytes"] == 512 * 1024 * 1024
                  and observation["pids_limit"] == 128 and observation["cpu_cores"] == 1.0)
            report["sandbox"] = check_sandbox(strict=True)
            check("actual strict sandbox execution", report["sandbox"]["run"]["ok"])
            if args.web_first:
                from web_first_browser_acceptance import run_browser_acceptance

                report["web"] = run_browser_acceptance(
                    output=args.output, root=root, env=env, launch=launch, request=request,
                )
                check("production web image browser acceptance", report["web"]["ok"])
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
            serialized = json.dumps(report, indent=2) + "\n"
            for value in sensitive:
                serialized = serialized.replace(value, "[REDACTED]")
            (args.output / "acceptance.json").write_text(serialized)
            hashes = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p in sorted(args.output.iterdir()) if p.is_file() and p.name != "SHA256SUMS"]
            (args.output / "SHA256SUMS").write_text("\n".join(hashes) + "\n")


if __name__ == "__main__":
    main()
