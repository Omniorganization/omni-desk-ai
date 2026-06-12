# Sandbox Runner Production

Run the sandbox runner outside the main OmniDesk application container. The app container must not mount or access the host Docker socket. Treat the runner as a separate execution boundary with its own host, VM, or rootless container runtime.

## Required Boundary

- `sandbox.backend=remote_docker` in the application config.
- `OMNIDESK_SANDBOX_RUNNER_TOKEN` set to a random secret of at least 32 characters.
- `OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET` set and shared only with the app.
- `OMNIDESK_SANDBOX_IMAGE` and `OMNIDESK_SANDBOX_IMAGE_ALLOWLIST` set to digest-pinned images.
- `OMNIDESK_SANDBOX_ALLOW_WORKSPACE_PATHS=0` unless the runner and workspace volume are on a dedicated isolated node.
- `OMNIDESK_SANDBOX_NONCE_DB=/var/lib/omnidesk-sandbox/nonces.sqlite3` so HMAC replay protection survives process restarts.
- Network access to the runner restricted to the OmniDesk app identity.

The default production protocol is workspace artifact upload: the app sends a bounded `workspace_archive_base64` tar.gz payload, and the runner extracts it only after rejecting path traversal, links, unsupported entries, file-count overflow, and oversized files. Do not expose arbitrary client filesystem paths across the runner RPC boundary. Shared volumes are an explicit isolated-node exception, not the default deployment model.

## Systemd Deployment

1. Install the application under `/opt/omnidesk`.
2. Create an unprivileged `omnidesk-sandbox` user.
3. Create `/var/lib/omnidesk-sandbox` owned by `omnidesk-sandbox`.
4. Copy `deploy/sandbox-runner.env.example` to `/etc/omnidesk/sandbox-runner.env` and replace all secrets.
5. Copy `deploy/sandbox-runner.service` to `/etc/systemd/system/omnidesk-sandbox-runner.service`.
6. Start with:

```bash
systemctl daemon-reload
systemctl enable --now omnidesk-sandbox-runner
```

## Readiness Smoke

`GET /ready` checks the configured container runtime, image allowlist, and, when `OMNIDESK_SANDBOX_READY_SMOKE=1`, executes a real no-network readonly sandbox command.

Run the same smoke from staging:

```bash
OMNIDESK_SMOKE_SANDBOX_URL=http://127.0.0.1:18890 \
OMNIDESK_SMOKE_SANDBOX_TOKEN="$OMNIDESK_SANDBOX_RUNNER_TOKEN" \
OMNIDESK_SMOKE_SANDBOX_HMAC_SECRET="$OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET" \
python scripts/production_smoke_test.py --sandbox-only
```

## Image Policy

The default sandbox image is:

```text
python:3.11-slim@sha256:f9fa7f851e38bfb19c9de3afbc4b86ae7176ea7aaf94535c31df5458d5849457
```

Review and rotate this digest intentionally. Do not use floating tags such as `python:3.11-slim` in production.
