#!/usr/bin/env bash
set -euo pipefail
IMAGE="${1:-omnidesk-agent:local}"
if command -v trivy >/dev/null 2>&1; then
  trivy image --severity HIGH,CRITICAL --exit-code 1 "$IMAGE"
elif docker scout version >/dev/null 2>&1; then
  docker scout cves --exit-code --only-severity critical,high "$IMAGE"
else
  echo "Install trivy or Docker Scout to scan image: $IMAGE" >&2
  exit 2
fi
