# Remote Sandbox Runner Architecture

Industrial deployments must not give the OmniDesk application container access to `/var/run/docker.sock`. Docker socket access is effectively host-level control.

Recommended layout:

```text
omnidesk-app
  - API / approval / queue / orchestration
  - no Docker socket
  - no sandbox execution privileges

omnidesk-sandbox-runner
  - isolated node or VM, or isolated runner container on a dedicated runner host
  - rootless Docker or Podman-compatible runtime socket
  - no application secrets
  - temporary workspace only
  - narrow GET /health, GET /ready, POST /v1/run API
  - authenticated with bearer token + optional HMAC timestamp/nonce signature
```

Production config:

```yaml
sandbox:
  backend: remote_docker
  runner_url: http://sandbox-runner:18890
  runner_token_env: OMNIDESK_SANDBOX_RUNNER_TOKEN
  docker_image: python:3.11-slim@sha256:f9fa7f851e38bfb19c9de3afbc4b86ae7176ea7aaf94535c31df5458d5849457
```

Deployment steps:

```bash
export PYTHON_BASE_IMAGE_DIGEST='python:3.11-slim@sha256:f9fa7f851e38bfb19c9de3afbc4b86ae7176ea7aaf94535c31df5458d5849457'
export OMNIDESK_SANDBOX_IMAGE='python:3.11-slim@sha256:f9fa7f851e38bfb19c9de3afbc4b86ae7176ea7aaf94535c31df5458d5849457'
export OMNIDESK_SANDBOX_IMAGE_ALLOWLIST="$OMNIDESK_SANDBOX_IMAGE"
export OMNIDESK_SANDBOX_RUNNER_TOKEN='<strong-token>'
export OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET='<strong-hmac-secret>'

python scripts/init_production_config.py \
  --public-base-url https://agent.example.com \
  --sandbox-image "$OMNIDESK_SANDBOX_IMAGE" \
  --runner-url http://sandbox-runner:18890

cd deploy/docker && docker compose config
cd ../sandbox-runner && docker compose config
```

The runner enforces:

```text
no network
read-only container rootfs
tmpfs /tmp
non-root user
--cap-drop ALL
no-new-privileges
pids/memory/cpu limits
image allowlist
command allowlist
output size limit
nonce replay protection when HMAC is enabled
workspace path constrained to OMNIDESK_SANDBOX_ALLOWED_WORKSPACE_ROOT
```

`/ready` checks that the configured container runtime is installed and usable. Set `OMNIDESK_SANDBOX_READY_SMOKE=1` to make readiness execute a real no-network sandbox smoke command.

For production smoke tests, set:

```bash
export OMNIDESK_SMOKE_SANDBOX_URL=http://sandbox-runner:18890
export OMNIDESK_SMOKE_SANDBOX_TOKEN="$OMNIDESK_SANDBOX_RUNNER_TOKEN"
export OMNIDESK_SMOKE_SANDBOX_HMAC_SECRET="$OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET"
python scripts/production_smoke_test.py
```
