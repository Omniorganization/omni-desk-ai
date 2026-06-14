#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: scripts/package_clean_zip.sh OUTPUT_ZIP [ROOT_DIR]" >&2
  exit 64
fi

OUTPUT_ZIP="$1"
ROOT_DIR="${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ROOT_DIR="$(cd "$ROOT_DIR" && pwd)"
PARENT_DIR="$(dirname "$ROOT_DIR")"
BASE_NAME="$(basename "$ROOT_DIR")"

cd "$ROOT_DIR"
find . -name ".DS_Store" -delete
find . -type d -name "__pycache__" -prune -exec rm -rf {} +
find . -type d -name ".pytest_cache" -prune -exec rm -rf {} +
find . -type d -name ".ruff_cache" -prune -exec rm -rf {} +
find . -type d -name ".mypy_cache" -prune -exec rm -rf {} +
find . -type f \( -name ".coverage" -o -name "coverage.json" -o -name "coverage.xml" -o -name "*.pyc" -o -name "*.pyo" \) -delete
rm -rf "$PARENT_DIR/__MACOSX"
chmod +x scripts/*.sh
python scripts/check_script_executability.py .
python scripts/check_release_hygiene.py . --allow-vcs

mkdir -p "$(dirname "$OUTPUT_ZIP")"
rm -f "$OUTPUT_ZIP"
(
  cd "$PARENT_DIR"
  COPYFILE_DISABLE=1 zip -rq "$OUTPUT_ZIP" "$BASE_NAME" \
    -x "*/.DS_Store" "__MACOSX/*" "*/.pytest_cache/*" "*/__pycache__/*" "*/.git/*" ".git/*" "*.pyc" "*.pyo" "*/coverage.json" "*/coverage.xml" "*/.coverage"
)

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
unzip -q "$OUTPUT_ZIP" -d "$TMP_DIR"
python "$ROOT_DIR/scripts/check_release_hygiene.py" "$TMP_DIR/$BASE_NAME"
python "$ROOT_DIR/scripts/check_script_executability.py" "$TMP_DIR/$BASE_NAME"
echo "Clean release zip written to $OUTPUT_ZIP"
