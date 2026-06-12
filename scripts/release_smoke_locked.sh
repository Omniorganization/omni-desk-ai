#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f requirements.lock ]]; then
  echo "requirements.lock is required for production release smoke tests" >&2
  exit 1
fi

wheel_count=$(find dist -maxdepth 1 -name '*.whl' | wc -l | tr -d ' ')
if [[ "$wheel_count" != "1" ]]; then
  echo "expected exactly one wheel in dist/, found $wheel_count" >&2
  exit 1
fi

venv_dir="${TMPDIR:-/tmp}/omnidesk-release-smoke-$$"
python -m venv "$venv_dir"
trap 'rm -rf "$venv_dir"' EXIT
"$venv_dir/bin/python" -m pip install --upgrade pip >/dev/null
python scripts/check_lock_hashes.py requirements.lock
"$venv_dir/bin/python" -m pip install --require-hashes -r requirements.lock >/dev/null
"$venv_dir/bin/python" -m pip install --no-deps dist/*.whl >/dev/null
"$venv_dir/bin/omnidesk" --help >/dev/null
"$venv_dir/bin/python" scripts/production_smoke_test.py --help >/dev/null || true
