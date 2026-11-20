#!/usr/bin/env bash
# One-time, idempotent setup of the single OmniDesk staging host.
#
# Run as root on the host from a checkout of the reviewed commit:
#   sudo bash deploy/staging/bootstrap-host.sh \
#     --public-host <dns name> --tls <acme email|internal> \
#     --app-image <repo>@sha256:<d> --runner-image <repo>@sha256:<d> \
#     --postgres-image postgres:16...@sha256:<d> --expected-version <v> \
#     --deploy-key-file <CI deploy public key> \
#     [--alert-receiver telegram --telegram-chat-id <id> | --alert-receiver webhook --webhook-url <url>] \
#     [--model-provider <p> --model <m>]
#
# It never prints secrets: application secrets are generated here and never
# leave the host. Operator secrets (model API key, alert bot token) are added
# later with `omnidesk-deploy-compose provision-secret NAME` reading stdin.
# Re-running keeps existing secrets and data; it re-renders configs and units.
set -euo pipefail
umask 077

usage() { sed -n '2,20p' "$0" >&2; exit 64; }

PUBLIC_HOST="" TLS="" APP_IMAGE="" RUNNER_IMAGE="" POSTGRES_IMAGE="" EXPECTED_VERSION="" DEPLOY_KEY_FILE=""
ALERT_RECEIVER="webhook" TELEGRAM_CHAT_ID="" WEBHOOK_URL="http://127.0.0.1:9/disabled" MODEL_PROVIDER="none" MODEL=""
ACME_CA="https://acme-v02.api.letsencrypt.org/directory"
SANDBOX_IMAGE="python:3.11-slim@sha256:174bec68e0451bffabbb08c7d5d21c6b253f772d81d52b9558af97bb3159b761"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --public-host) PUBLIC_HOST="$2"; shift 2 ;;
    --tls) TLS="$2"; shift 2 ;;
    --acme-ca) ACME_CA="$2"; shift 2 ;;
    --app-image) APP_IMAGE="$2"; shift 2 ;;
    --runner-image) RUNNER_IMAGE="$2"; shift 2 ;;
    --postgres-image) POSTGRES_IMAGE="$2"; shift 2 ;;
    --expected-version) EXPECTED_VERSION="$2"; shift 2 ;;
    --deploy-key-file) DEPLOY_KEY_FILE="$2"; shift 2 ;;
    --alert-receiver) ALERT_RECEIVER="$2"; shift 2 ;;
    --telegram-chat-id) TELEGRAM_CHAT_ID="$2"; shift 2 ;;
    --webhook-url) WEBHOOK_URL="$2"; shift 2 ;;
    --model-provider) MODEL_PROVIDER="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --sandbox-image) SANDBOX_IMAGE="$2"; shift 2 ;;
    *) usage ;;
  esac
done

digest_ref='^[a-z0-9][a-z0-9._/:-]{0,200}@sha256:[0-9a-f]{64}$'
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 64; }
[[ "$PUBLIC_HOST" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] || { echo "--public-host must be a DNS name" >&2; exit 64; }
[[ "$TLS" == "internal" || "$TLS" =~ ^[^[:space:]@]+@[^[:space:]@]+$ ]] || { echo "--tls must be an ACME account email or 'internal'" >&2; exit 64; }
for ref in "$APP_IMAGE" "$RUNNER_IMAGE" "$POSTGRES_IMAGE" "$SANDBOX_IMAGE"; do
  [[ "$ref" =~ $digest_ref ]] || { echo "images must be pinned by digest: $ref" >&2; exit 64; }
done
[[ "$EXPECTED_VERSION" =~ ^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$ ]] || { echo "invalid --expected-version" >&2; exit 64; }
[[ -f "$DEPLOY_KEY_FILE" ]] && grep -Eq '^ssh-ed25519 [A-Za-z0-9+/]+={0,3}( .*)?$' "$DEPLOY_KEY_FILE" || { echo "--deploy-key-file must hold one ssh-ed25519 public key" >&2; exit 64; }
command -v docker >/dev/null && command -v podman >/dev/null && docker compose version >/dev/null || { echo "docker, docker compose and podman are required (cloud-init installs them)" >&2; exit 69; }
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
STATE=/etc/omnidesk/staging COMPOSE=/opt/omnidesk/staging

echo "== rootless containers on Ubuntu 24.04"
# Ubuntu 24.04 lets only AppArmor-profiled programs create unprivileged user
# namespaces. Grant that to podman alone instead of lifting the restriction
# for every program on the host.
if [[ "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" == "1" ]]; then
  install -m 0644 "$REPO/deploy/staging/apparmor-podman" /etc/apparmor.d/omnidesk-podman
  apparmor_parser -r /etc/apparmor.d/omnidesk-podman
fi

echo "== users"
id omnidesk-deploy >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/omnidesk-deploy --shell /bin/bash omnidesk-deploy
id omnidesk-runner >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/omnidesk-runner --shell /usr/sbin/nologin omnidesk-runner
RUNNER_UID="$(id -u omnidesk-runner)" RUNNER_GID="$(id -g omnidesk-runner)"
grep -q "^omnidesk-runner:" /etc/subuid || usermod --add-subuids 200000-265535 --add-subgids 200000-265535 omnidesk-runner
loginctl enable-linger omnidesk-runner
for _ in $(seq 1 30); do [[ -d "/run/user/$RUNNER_UID" ]] && break; sleep 1; done
runner_systemctl() { sudo -u omnidesk-runner XDG_RUNTIME_DIR="/run/user/$RUNNER_UID" systemctl --user "$@"; }
runner_systemctl enable --now podman.socket
SOCKET="/run/user/$RUNNER_UID/podman/podman.sock"
for _ in $(seq 1 30); do [[ -S "$SOCKET" ]] && break; sleep 1; done
[[ -S "$SOCKET" ]] || { echo "rootless podman socket did not appear at $SOCKET" >&2; exit 70; }
[[ "$(sudo -u omnidesk-runner XDG_RUNTIME_DIR="/run/user/$RUNNER_UID" podman info --format '{{.Host.Security.Rootless}}')" == "true" ]] || { echo "runner podman is not rootless" >&2; exit 70; }
sudo -u omnidesk-runner XDG_RUNTIME_DIR="/run/user/$RUNNER_UID" podman pull --quiet "$SANDBOX_IMAGE" >/dev/null

echo "== directories"
install -d -m 0755 -o root -g root /opt/omnidesk "$COMPOSE" "$COMPOSE/observability" "$COMPOSE/scripts"
install -d -m 0700 -o root -g root /etc/omnidesk "$STATE" /var/lib/omnidesk/backups
install -d -m 0755 -o root -g root /var/lib/omnidesk /var/lib/omnidesk/textfile /var/log/omnidesk
install -d -m 0700 -o "$RUNNER_UID" -g "$RUNNER_GID" /srv/omnidesk-sandbox-workspaces /srv/omnidesk-sandbox-workspaces/tmp /var/lib/omnidesk/runner-state

echo "== compose project"
install -m 0644 "$REPO/deploy/docker/docker-compose.full.yml" "$COMPOSE/docker-compose.full.yml"
install -m 0644 "$REPO/deploy/staging/compose.staging.yml" "$COMPOSE/compose.staging.yml"
install -m 0644 "$REPO/deploy/staging/Caddyfile" "$COMPOSE/Caddyfile"
install -m 0644 "$REPO/deploy/staging/prometheus.staging.yml" "$COMPOSE/prometheus.staging.yml"
install -m 0644 "$REPO/deploy/observability/prometheus-rules.yml" "$REPO/deploy/observability/prometheus-rules.host.yml" "$COMPOSE/observability/"
install -m 0644 "$REPO/scripts/production_smoke_test.py" "$COMPOSE/scripts/production_smoke_test.py"
install -m 0755 "$REPO/deploy/staging/omnidesk-deploy-compose" /usr/local/bin/omnidesk-deploy-compose
install -m 0755 "$REPO/deploy/staging/omnidesk-ssh-entry" /usr/local/bin/omnidesk-ssh-entry
install -m 0440 "$REPO/deploy/staging/sudoers" /etc/sudoers.d/omnidesk-deploy
visudo -cf /etc/sudoers.d/omnidesk-deploy >/dev/null

echo "== secrets (generated once, kept on re-run)"
token() { python3 -c 'import secrets; print(secrets.token_urlsafe(48))'; }
if [[ ! -s "$STATE/compose.env" ]]; then
  pg_password="$(token)"
  fernet="$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
  dsn="postgresql://omnidesk:${pg_password}@omnidesk-postgres:5432/omnidesk"
  {
    for name in ADMIN_TOKEN VIEWER_TOKEN GATEWAY_SECRET APPSYNC_SECRET_PEPPER PLUGIN_SIGNING_SECRET \
                AUDIT_CHECKPOINT_HMAC_KEY SANDBOX_RUNNER_TOKEN SANDBOX_RUNNER_HMAC_SECRET; do
      echo "OMNIDESK_${name}=$(token)"
    done
    echo "OMNIDESK_MEMORY_ENCRYPTION_KEY=${fernet}"
    echo "OMNIDESK_POSTGRES_DSN=${dsn}"
    echo "OMNIDESK_APPSYNC_POSTGRES_DSN=${dsn}"
  } > "$STATE/compose.env"
  printf '%s' "$pg_password" > "$STATE/postgres_password"
fi
chmod 0600 "$STATE/compose.env"; chmod 0400 "$STATE/postgres_password"
# Static (non-secret) values: replaced on every run.
python3 - "$STATE/compose.env" <<PY
import sys
path = sys.argv[1]
static = {
    "OMNIDESK_POSTGRES_IMAGE": "$POSTGRES_IMAGE", "OMNIDESK_POSTGRES_DB": "omnidesk", "OMNIDESK_POSTGRES_USER": "omnidesk",
    "OMNIDESK_POSTGRES_PASSWORD_FILE": "$STATE/postgres_password",
    "OMNIDESK_SANDBOX_RUNNER_IMAGE": "$RUNNER_IMAGE", "OMNIDESK_SANDBOX_IMAGE_ALLOWLIST": "$SANDBOX_IMAGE",
    "OMNIDESK_CONTAINER_RUNTIME": "docker", "OMNIDESK_CONTAINER_RUNTIME_HOST": "unix:///run/podman/podman.sock",
    "OMNIDESK_CONTAINER_SOCKET_PATH": "$SOCKET", "OMNIDESK_RUNNER_UID": "$RUNNER_UID", "OMNIDESK_RUNNER_GID": "$RUNNER_GID",
    "OMNIDESK_RUNNER_STATE_DIR": "/var/lib/omnidesk/runner-state", "OMNIDESK_STAGING_CONFIG": "$STATE/config.production.yaml",
    "OMNIDESK_STORAGE_NAMESPACE": "staging", "OMNIDESK_PUBLIC_HOST": "$PUBLIC_HOST", "OMNIDESK_TLS": "$TLS",
    "OMNIDESK_ACME_CA": "$ACME_CA", "OMNIDESK_METRICS_TOKEN_FILE": "$STATE/metrics-viewer-token",
    "OMNIDESK_ALERTMANAGER_CONFIG": "$STATE/alertmanager.yml",
    "OMNIDESK_ALERT_TELEGRAM_BOT_TOKEN_FILE": "$STATE/alert-telegram-bot-token",
    "OMNIDESK_TEXTFILE_DIR": "/var/lib/omnidesk/textfile",
}
values = {}
for line in open(path, encoding="utf-8").read().splitlines():
    if "=" in line and not line.startswith("#"):
        key, value = line.split("=", 1)
        values[key] = value
values.update(static)
with open(path, "w", encoding="utf-8") as handle:
    handle.writelines(f"{key}={values[key]}\n" for key in sorted(values))
PY
viewer="$(grep '^OMNIDESK_VIEWER_TOKEN=' "$STATE/compose.env" | cut -d= -f2-)"
printf '%s' "$viewer" > "$STATE/metrics-viewer-token"
chown 65534:65534 "$STATE/metrics-viewer-token"; chmod 0400 "$STATE/metrics-viewer-token"
if [[ ! -e "$STATE/alert-telegram-bot-token" ]]; then
  printf 'not-provisioned' > "$STATE/alert-telegram-bot-token"
  chown 65534:65534 "$STATE/alert-telegram-bot-token"; chmod 0400 "$STATE/alert-telegram-bot-token"
fi

echo "== release"
if [[ ! -s "$STATE/release.env" ]]; then
  printf 'OMNIDESK_EXPECTED_VERSION=%s\nOMNIDESK_IMAGE=%s\nOMNIDESK_IMAGE_DIGEST=%s\n' \
    "$EXPECTED_VERSION" "$APP_IMAGE" "${APP_IMAGE##*@}" > "$STATE/release.env"
  chmod 0600 "$STATE/release.env"
fi
docker pull --quiet "$APP_IMAGE" >/dev/null
docker pull --quiet "$RUNNER_IMAGE" >/dev/null
docker pull --quiet "$POSTGRES_IMAGE" >/dev/null

echo "== gateway and alertmanager config (validated by the release's own code)"
docker run --rm -i --env-file "$STATE/compose.env" -e OMNIDESK_STAGING_BASE_CONFIG="$(cat "$REPO/deploy/docker/config.production.example.yaml")" \
  -e OMNIDESK_ENV=production -e OMNIDESK_REQUIRE_PRODUCTION_GUARDS=1 -e OMNIDESK_RUNNING_IN_CONTAINER=1 \
  "$APP_IMAGE" python - --public-host "$PUBLIC_HOST" --sandbox-image "$SANDBOX_IMAGE" \
  --model-provider "$MODEL_PROVIDER" --model "$MODEL" < "$REPO/deploy/staging/render_staging_config.py" > "$STATE/config.production.yaml.new"
mv "$STATE/config.production.yaml.new" "$STATE/config.production.yaml"; chmod 0644 "$STATE/config.production.yaml"
receiver_args=(--receiver "$ALERT_RECEIVER")
if [[ "$ALERT_RECEIVER" == "telegram" ]]; then receiver_args+=(--telegram-chat-id "$TELEGRAM_CHAT_ID"); else receiver_args+=(--webhook-url "$WEBHOOK_URL"); fi
docker run --rm -v "$REPO/deploy:/deploy:ro" -v "$REPO/scripts:/scripts:ro" -v "$STATE:/out" --user 0 --entrypoint python "$APP_IMAGE" \
  /scripts/render_alertmanager_config.py "${receiver_args[@]}" --output /out/alertmanager.yml >/dev/null
chmod 0644 "$STATE/alertmanager.yml"

echo "== deploy identity for CI (SSH, forced command, sudo for one helper only)"
install -d -m 0700 -o omnidesk-deploy -g omnidesk-deploy /var/lib/omnidesk-deploy/.ssh
printf 'restrict,command="/usr/local/bin/omnidesk-ssh-entry" %s\n' "$(head -n1 "$DEPLOY_KEY_FILE")" > /var/lib/omnidesk-deploy/.ssh/authorized_keys
chown omnidesk-deploy:omnidesk-deploy /var/lib/omnidesk-deploy/.ssh/authorized_keys; chmod 0600 /var/lib/omnidesk-deploy/.ssh/authorized_keys

echo "== systemd units"
install -m 0644 "$REPO/deploy/staging/systemd/"*.service "$REPO/deploy/staging/systemd/"*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable omnidesk-staging.service omnidesk-postgres-backup.timer omnidesk-postgres-maintenance.timer
systemctl start omnidesk-postgres-backup.timer omnidesk-postgres-maintenance.timer

echo "== start"
/usr/local/bin/omnidesk-deploy-compose up
echo "bootstrap complete for https://$PUBLIC_HOST (secrets stayed on this host)"
