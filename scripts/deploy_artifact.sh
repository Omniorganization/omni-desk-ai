#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/deploy_artifact.sh --artifact PATH --environment staging|production --mode docker-compose|kubectl|systemd|noop [--expected-version VERSION] [--artifact-sha256 SHA256] [--image-digest sha256:DIGEST]

Environment variables by mode:
  docker-compose: OMNIDESK_DEPLOY_COMPOSE_FILE, OMNIDESK_DEPLOY_SERVICE
  kubectl:        OMNIDESK_DEPLOY_KUBE_CONTEXT, OMNIDESK_DEPLOY_NAMESPACE, OMNIDESK_DEPLOYMENT_NAME, OMNIDESK_CONTAINER_NAME, OMNIDESK_IMAGE
  systemd:        OMNIDESK_DEPLOY_HOST, OMNIDESK_DEPLOY_USER, OMNIDESK_REMOTE_DEPLOY_SCRIPT, optional OMNIDESK_DEPLOY_SERVICE
  noop:           staging-only dry-run; validates artifact only
USAGE
}

is_sha256() {
  [[ "$1" =~ ^[0-9a-f]{64}$ ]]
}

safe_token() {
  [[ "$1" =~ ^[A-Za-z0-9_.@:/+=-]+$ ]]
}

compose_service_image() {
  local compose_file="$1"
  local service="$2"
  docker compose -f "$compose_file" config | "${PYTHON:-python3}" -c '
import re
import sys

service = sys.argv[1]
in_services = False
in_target = False
for raw in sys.stdin:
    line = raw.rstrip("\n")
    if re.match(r"^services:\s*$", line):
        in_services = True
        continue
    if not in_services:
        continue
    service_match = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
    if service_match:
        in_target = service_match.group(1) == service
        continue
    if in_target:
        image_match = re.match(r"^    image:\s*(.+?)\s*$", line)
        if image_match:
            print(image_match.group(1).strip().strip("\"'\''"))
            raise SystemExit(0)
raise SystemExit(1)
' "$service"
}

ARTIFACT=""
ENVIRONMENT=""
EXPECTED_VERSION="${OMNIDESK_EXPECTED_VERSION:-}"
ARTIFACT_SHA256="${OMNIDESK_ARTIFACT_SHA256:-}"
IMAGE_DIGEST="${OMNIDESK_IMAGE_DIGEST:-}"
MODE="${OMNIDESK_DEPLOY_MODE:-noop}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --artifact) ARTIFACT="${2:-}"; shift 2 ;;
    --environment) ENVIRONMENT="${2:-}"; shift 2 ;;
    --mode) MODE="${2:-}"; shift 2 ;;
    --expected-version) EXPECTED_VERSION="${2:-}"; shift 2 ;;
    --artifact-sha256) ARTIFACT_SHA256="${2:-}"; shift 2 ;;
    --image-digest) IMAGE_DIGEST="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 64 ;;
  esac
done

if [[ -z "$ARTIFACT" || -z "$ENVIRONMENT" ]]; then
  usage
  exit 64
fi
if [[ "$ENVIRONMENT" != "staging" && "$ENVIRONMENT" != "production" ]]; then
  echo "environment must be staging or production" >&2
  exit 64
fi
if [[ ! -f "$ARTIFACT" ]]; then
  echo "artifact not found: $ARTIFACT" >&2
  exit 66
fi
if [[ "$ENVIRONMENT" = "production" && "$MODE" = "noop" ]]; then
  echo "noop deploy mode is forbidden for production promotion" >&2
  exit 64
fi

actual_sha="$(sha256sum "$ARTIFACT" | awk '{print $1}')"
if [[ -n "$ARTIFACT_SHA256" && "$actual_sha" != "$ARTIFACT_SHA256" ]]; then
  echo "artifact sha256 mismatch: expected $ARTIFACT_SHA256 got $actual_sha" >&2
  exit 65
fi
if [[ -n "$ARTIFACT_SHA256" ]] && ! is_sha256 "$ARTIFACT_SHA256"; then
  echo "artifact sha256 must be a lowercase 64-character sha256 digest" >&2
  exit 64
fi
if [[ -n "$IMAGE_DIGEST" && ! "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "image digest must be a digest like sha256:<64 hex chars>" >&2
  exit 64
fi

case "$MODE" in
  noop)
    echo "Validated artifact for $ENVIRONMENT deployment: $ARTIFACT sha256=$actual_sha"
    ;;
  docker-compose)
    : "${OMNIDESK_DEPLOY_COMPOSE_FILE:?OMNIDESK_DEPLOY_COMPOSE_FILE is required}"
    : "${OMNIDESK_DEPLOY_SERVICE:?OMNIDESK_DEPLOY_SERVICE is required}"
    if [[ "$ENVIRONMENT" = "production" ]]; then
      : "${IMAGE_DIGEST:?OMNIDESK_IMAGE_DIGEST or --image-digest is required for production docker-compose deploy}"
    fi
    export OMNIDESK_ARTIFACT_SHA256="$actual_sha"
    export OMNIDESK_EXPECTED_VERSION="$EXPECTED_VERSION"
    if [[ -n "$IMAGE_DIGEST" ]]; then export OMNIDESK_IMAGE_DIGEST="$IMAGE_DIGEST"; fi
    compose_image="$(compose_service_image "$OMNIDESK_DEPLOY_COMPOSE_FILE" "$OMNIDESK_DEPLOY_SERVICE")" || {
      echo "could not resolve docker-compose image for service: $OMNIDESK_DEPLOY_SERVICE" >&2
      exit 65
    }
    if [[ "$ENVIRONMENT" = "production" && ! "$compose_image" =~ @sha256:[0-9a-f]{64}$ ]]; then
      echo "production docker-compose deploy requires service image pinned by digest" >&2
      exit 64
    fi
    if [[ -n "$IMAGE_DIGEST" && "$compose_image" != *"@$IMAGE_DIGEST" ]]; then
      echo "docker-compose service image digest does not match expected image digest" >&2
      exit 65
    fi
    docker compose -f "$OMNIDESK_DEPLOY_COMPOSE_FILE" pull "$OMNIDESK_DEPLOY_SERVICE"
    docker compose -f "$OMNIDESK_DEPLOY_COMPOSE_FILE" up -d "$OMNIDESK_DEPLOY_SERVICE"
    ;;
  kubectl)
    : "${OMNIDESK_DEPLOY_KUBE_CONTEXT:?OMNIDESK_DEPLOY_KUBE_CONTEXT is required}"
    : "${OMNIDESK_DEPLOY_NAMESPACE:?OMNIDESK_DEPLOY_NAMESPACE is required}"
    : "${OMNIDESK_DEPLOYMENT_NAME:?OMNIDESK_DEPLOYMENT_NAME is required}"
    : "${OMNIDESK_CONTAINER_NAME:?OMNIDESK_CONTAINER_NAME is required}"
    : "${OMNIDESK_IMAGE:?OMNIDESK_IMAGE is required}"
    if [[ "$ENVIRONMENT" = "production" && ! "$OMNIDESK_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]]; then
      echo "production kubectl deploy requires OMNIDESK_IMAGE pinned by digest" >&2
      exit 64
    fi
    if [[ -n "$IMAGE_DIGEST" && "$OMNIDESK_IMAGE" != *"@$IMAGE_DIGEST" ]]; then
      echo "OMNIDESK_IMAGE digest does not match expected image digest" >&2
      exit 65
    fi
    kubectl --context "$OMNIDESK_DEPLOY_KUBE_CONTEXT" -n "$OMNIDESK_DEPLOY_NAMESPACE" set image "deployment/$OMNIDESK_DEPLOYMENT_NAME" "$OMNIDESK_CONTAINER_NAME=$OMNIDESK_IMAGE"
    kubectl --context "$OMNIDESK_DEPLOY_KUBE_CONTEXT" -n "$OMNIDESK_DEPLOY_NAMESPACE" rollout status "deployment/$OMNIDESK_DEPLOYMENT_NAME" --timeout=180s
    ;;
  systemd)
    : "${OMNIDESK_DEPLOY_HOST:?OMNIDESK_DEPLOY_HOST is required}"
    : "${OMNIDESK_DEPLOY_USER:?OMNIDESK_DEPLOY_USER is required}"
    REMOTE_SCRIPT="${OMNIDESK_REMOTE_DEPLOY_SCRIPT:-/usr/local/bin/omnidesk-deploy-artifact}"
    if [[ "$REMOTE_SCRIPT" != /usr/local/bin/* ]]; then
      echo "OMNIDESK_REMOTE_DEPLOY_SCRIPT must be under /usr/local/bin" >&2
      exit 64
    fi
    if ! safe_token "$OMNIDESK_DEPLOY_HOST" || ! safe_token "$OMNIDESK_DEPLOY_USER" || ! safe_token "$REMOTE_SCRIPT"; then
      echo "unsafe systemd deploy target or script" >&2
      exit 64
    fi
    remote_artifact="/tmp/omnidesk-release-artifact-${actual_sha}.whl"
    scp "$ARTIFACT" "$OMNIDESK_DEPLOY_USER@$OMNIDESK_DEPLOY_HOST:$remote_artifact"
    ssh "$OMNIDESK_DEPLOY_USER@$OMNIDESK_DEPLOY_HOST" \
      "sudo '$REMOTE_SCRIPT' --artifact '$remote_artifact' --sha256 '$actual_sha' --expected-version '$EXPECTED_VERSION' --image-digest '$IMAGE_DIGEST'"
    ;;
  *)
    echo "unsupported deploy mode: $MODE" >&2
    exit 64
    ;;
esac
