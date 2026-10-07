"""Host setup and recovery for a reviewed Web/Gateway pilot. Never provisions or purchases a host."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import subprocess
from urllib.parse import urlsplit

import yaml
from cryptography.fernet import Fernet


def validate_origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("A real HTTPS origin without credentials is required")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.port not in (None, 443):
        raise ValueError("Public ingress must use HTTPS 443 on an origin without a path")
    if parsed.hostname.endswith((".invalid", ".example", ".internal")) or parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Placeholder/loopback origin cannot be production")
    if not re.fullmatch(r"[a-zA-Z0-9.-]+", parsed.hostname):
        raise ValueError("Use a DNS hostname or public IPv4 HTTPS origin")
    return "https://" + parsed.netloc


def private_write(path: Path, value: str, *, user_id: int, group_id: int) -> None:
    # Exclusive creation prevents overwriting identities and live credentials.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(value)
    os.chown(path, user_id, group_id)


def init(args) -> None:
    if os.geteuid() != 0:
        raise ValueError("Host setup requires the host administrator")
    origin = validate_origin(args.origin)
    account = pwd.getpwnam(args.user)
    if account.pw_uid == 0 or not re.fullmatch(r"[a-z_][a-z0-9_-]*", args.user):
        raise ValueError("A dedicated unprivileged Linux account is required")
    dsn = os.environ.get(args.dsn_env, "")
    if not dsn.startswith(("postgresql://", "postgres://")) or any(c in dsn for c in "\r\n\"\\"):
        raise ValueError("Provide a valid private PostgreSQL DSN in the named environment variable")
    for path in (args.cert, args.key):
        if not path.is_file() or not path.is_absolute() or re.search(r"[\s;{}]", str(path)):
            raise ValueError("Existing TLS certificate/key absolute paths are required")
    hostname = urlsplit(origin).hostname
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        identity_option = "-verify_hostname"
    else:
        if not address.is_global:
            raise ValueError("Production IP origin must be publicly routable")
        identity_option = "-verify_ip"
    subprocess.run(["openssl", "verify", identity_option, hostname,
                    "-untrusted", str(args.cert), str(args.cert)], check=True, capture_output=True)
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    certificate = x509.load_pem_x509_certificate(args.cert.read_bytes())
    private_key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
    public_bytes = lambda key: key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if public_bytes(certificate.public_key()) != public_bytes(private_key.public_key()):
        raise ValueError("TLS certificate and private key do not match")
    if not args.gateway_only and not re.fullmatch(r"sha256:[a-f0-9]{64}", args.web_image_id or ""):
        raise ValueError("Use the verified immutable Web image ID")
    destination = Path("/etc/omnidesk/web-first")
    destination.mkdir(mode=0o750, parents=True, exist_ok=True)
    if any((destination / name).exists() for name in ("runtime.env", "web.env", "config.yaml")):
        raise ValueError("Existing deployment secrets/configuration must not be overwritten")
    os.chown(destination, 0, account.pw_gid)
    os.chmod(destination, 0o750)
    state = Path("/var/lib/omnidesk-web-first")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(state, account.pw_uid, account.pw_gid)
    (state / "backups").mkdir(mode=0o700, exist_ok=True)
    os.chown(state / "backups", account.pw_uid, account.pw_gid)
    values = {"OMNIDESK_" + name: secrets.token_urlsafe(48) for name in (
        "ADMIN_TOKEN", "VIEWER_TOKEN", "OPERATOR_TOKEN", "OWNER_TOKEN", "GATEWAY_SECRET",
        "PLUGIN_SIGNING_SECRET", "APPSYNC_SECRET_PEPPER", "SANDBOX_RUNNER_TOKEN",
        "SANDBOX_RUNNER_HMAC_SECRET", "AUDIT_CHECKPOINT_HMAC_KEY")}
    values.update({"OMNIDESK_MEMORY_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                   "OMNIDESK_ENV": "production", "OMNIDESK_REQUIRE_PRODUCTION_GUARDS": "true",
                   "OMNIDESK_POSTGRES_DSN": dsn, "OMNIDESK_APPSYNC_POSTGRES_DSN": dsn,
                   "OMNIDESK_CONTAINER_RUNTIME": "podman", "OMNIDESK_SANDBOX_RUNNER_HOST": "127.0.0.1",
                   "OMNIDESK_SANDBOX_READY_SMOKE": "1", "OMNIDESK_SANDBOX_ALLOW_WORKSPACE_PATHS": "0",
                   "OMNIDESK_SANDBOX_NONCE_DB": str(state / "runner-nonces.sqlite3"),
                   "OMNIDESK_SANDBOX_ALLOWED_WORKSPACE_ROOT": str(state / "sandbox-workspaces")})
    for role in ("ADMIN", "VIEWER", "OPERATOR", "OWNER"):
        values["OMNIDESK_" + role + "_ACTOR"] = args.actor
    if not re.fullmatch(r"[A-Za-z0-9@._:-]{1,128}", args.actor):
        raise ValueError("Use a bounded actor identity")
    runtime = "\n".join(f'{name}="{value}"' for name, value in sorted(values.items())) + "\n"
    config = yaml.safe_load(Path("deploy/docker/config.production.example.yaml").read_text())

    def relocate(value):
        if isinstance(value, dict):
            return {key: relocate(item) for key, item in value.items()}
        if isinstance(value, list):
            return [relocate(item) for item in value]
        if isinstance(value, str) and value.startswith(("/data/", "/opt/")):
            return str(state / value.lstrip("/"))
        return value

    config = relocate(config)
    config["gateway"].update(host="127.0.0.1", public_base_url=origin, admin_allowed_ips=["127.0.0.1"])
    config["sandbox"]["runner_url"] = "http://127.0.0.1:18890"
    config["models"]["default"] = "local"
    profile = {"provider": "ollama", "model": args.model, "api_key_env": None,
               "base_url": "http://127.0.0.1:11434", "max_output_tokens": 256}
    config["models"]["profiles"] = {name: dict(profile) for name in ("fast", "planner", "local", "code")}
    config["runtime"]["required_ollama_models"] = [args.model]
    config["models"]["budget"].update(daily_usd_limit=0.01, monthly_usd_limit=0.01,
                                       per_actor_daily_usd_limit=0.01, on_exceed="block")
    from omnidesk_agent.config import AppConfig
    from omnidesk_agent.validation.production import validate_production_config

    policy = validate_production_config(AppConfig.model_validate(config), dict(os.environ, **values))
    if not policy.get("ok"):
        raise ValueError("Production policy refused host configuration; inspect on-host diagnostics")
    private_write(destination / "runtime.env", runtime, user_id=account.pw_uid, group_id=account.pw_gid)
    if not args.gateway_only:
        web = {"NODE_ENV": "production", "HOSTNAME": "127.0.0.1", "PORT": "13000",
               "OMNI_WEB_PUBLIC_ORIGIN": origin, "OMNI_GATEWAY_URL": "http://127.0.0.1:18789",
               "OMNI_WEB_SESSION_MAX_AGE_SECONDS": "3600", "OMNI_WEB_IMAGE_ID": args.web_image_id}
        private_write(destination / "web.env", "\n".join(f'{name}={value}' for name, value in web.items()) + "\n",
                      user_id=account.pw_uid, group_id=account.pw_gid)
    private_write(destination / "config.yaml", yaml.safe_dump(config), user_id=account.pw_uid, group_id=account.pw_gid)
    template_name = "nginx.gateway-only.conf.template" if args.gateway_only else "nginx.conf.template"
    template = (Path("deploy/web-first") / template_name).read_text()
    nginx = template.replace("@HOST@", urlsplit(origin).hostname).replace("@AUTHORITY@", urlsplit(origin).netloc)
    nginx = nginx.replace("@CERT@", str(args.cert)).replace("@KEY@", str(args.key))
    private_write(destination / "nginx.conf", nginx, user_id=0, group_id=0)
    services = ["omnidesk-web-gateway.service", "omnidesk-web-runner.service"]
    if not args.gateway_only:
        services.insert(0, "omnidesk-web-first.service")
    for filename in services:
        service = (Path("deploy/web-first") / filename).read_text().replace("@USER@", args.user).replace("@UID@", str(account.pw_uid))
        private_write(destination / filename, service, user_id=0, group_id=0)
    print("Private configuration generated; no credentials printed. Services are not activated.")


def backup(args) -> None:
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        raise ValueError("Private database DSN is required")
    output = args.output.resolve()
    if output.exists() or not output.parent.is_dir():
        raise ValueError("Use a new backup path in a private existing directory")
    environment = dict(os.environ, PGDATABASE=dsn)
    previous = os.umask(0o077)
    try:
        subprocess.run(["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--file", str(output)],
                       check=True, env=environment, capture_output=True)
    finally:
        os.umask(previous)
    with output.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    (output.with_suffix(output.suffix + ".sha256")).write_text(checksum + "\n")
    print(json.dumps({"backup_bytes": output.stat().st_size, "sha256": checksum}))


def restore(args) -> None:
    source = os.environ.get(args.source_dsn_env)
    target = os.environ.get(args.target_dsn_env)
    if not source or not target or source == target or urlsplit(source).path == urlsplit(target).path:
        raise ValueError("Restore requires a separately named database and distinct source/target DSNs")
    with args.backup.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    if checksum != args.backup.with_suffix(args.backup.suffix + ".sha256").read_text().strip():
        raise ValueError("Backup digest mismatch")
    environment = dict(os.environ, PGDATABASE=target)
    empty = subprocess.run(["psql", "--no-psqlrc", "--tuples-only", "--no-align", "--command",
                            "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema');"],
                           check=True, env=environment, text=True, capture_output=True)
    if empty.stdout.strip() != "0":
        raise ValueError("Restore target must be an empty independent database")
    subprocess.run(["pg_restore", "--exit-on-error", "--no-owner", "--no-acl", "--dbname", "", str(args.backup)],
                   check=True, env=environment, capture_output=True)
    print("Restored into independent database; validate migrations, messages and durable nonces before switching traffic.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    setup = actions.add_parser("init")
    setup.add_argument("--origin", required=True)
    setup.add_argument("--user", required=True)
    setup.add_argument("--actor", required=True)
    setup.add_argument("--dsn-env", default="OMNIDESK_POSTGRES_DSN")
    setup.add_argument("--cert", type=Path, required=True)
    setup.add_argument("--key", type=Path, required=True)
    mode = setup.add_mutually_exclusive_group(required=True)
    mode.add_argument("--web-image-id", help="Verified immutable image ID for the default Web-first mode")
    mode.add_argument("--gateway-only", action="store_true", help="Generate only the private Gateway/runner and finite HTTPS API ingress")
    setup.add_argument("--model", default="smollm2:135m")
    dump = actions.add_parser("backup")
    dump.add_argument("--dsn-env", default="OMNIDESK_POSTGRES_DSN")
    dump.add_argument("--output", type=Path, required=True)
    recovery = actions.add_parser("restore")
    recovery.add_argument("--source-dsn-env", default="OMNIDESK_POSTGRES_DSN")
    recovery.add_argument("--target-dsn-env", default="OMNIDESK_RESTORE_POSTGRES_DSN")
    recovery.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()
    try:
        {"init": init, "backup": backup, "restore": restore}[args.action](args)
    except subprocess.CalledProcessError:
        # pg/host diagnostics can contain credentials: keep them off shared CI/stdout.
        raise SystemExit("Host operation failed; inspect private on-host diagnostics") from None


if __name__ == "__main__":
    main()
