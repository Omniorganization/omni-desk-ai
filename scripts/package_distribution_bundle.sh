#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${OUT_DIR:-dist/distribution}"
VERSION="${VERSION:-1.12.7+root-monorepo-production-ga-candidate}"
SOURCE_COMMIT="${SOURCE_COMMIT:-unknown}"

usage() {
  echo "Usage: scripts/package_distribution_bundle.sh [--out-dir DIR] [--version VERSION] [--source-commit COMMIT]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out-dir) OUT_DIR="${2:-}"; shift 2 ;;
    --version) VERSION="${2:-}"; shift 2 ;;
    --source-commit) SOURCE_COMMIT="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

if [[ "$OUT_DIR" != /* ]]; then
  OUT_DIR="${ROOT_DIR}/${OUT_DIR}"
fi

if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync is required to build clean split distribution zips" >&2
  exit 127
fi
if ! command -v zip >/dev/null 2>&1; then
  echo "zip is required to build distribution zips" >&2
  exit 127
fi

NATIVE_VERSION="${VERSION%%+*}"
VERSION_SUFFIX="${VERSION#*+}"
if [[ "$VERSION_SUFFIX" == "$VERSION" ]]; then
  VERSION_SUFFIX="source"
fi
SLUG="Omni-desk-AI-${NATIVE_VERSION}-${VERSION_SUFFIX//+/-}"
WORK_DIR="${OUT_DIR}/.staging-${SLUG}"
PACKAGE_DIR="${OUT_DIR}/${SLUG}"

rsync_common=(
  -a
  --exclude .git
  --exclude .venv
  --exclude .next
  --exclude .npm-cache
  --exclude .pytest_cache
  --exclude .ruff_cache
  --exclude .mypy_cache
  --exclude .serena
  --exclude .dart_tool
  --exclude __pycache__
  --exclude __MACOSX
  --exclude 'Omni-desk-AI-*-source-gated-*'
  --exclude node_modules
  --exclude build
  --exclude dist
  --exclude target
  --exclude coverage
  --exclude htmlcov
  --exclude '*.pyc'
  --exclude '*.pyo'
  --exclude '*.tsbuildinfo'
  --exclude .DS_Store
  --exclude .coverage
  --exclude coverage.json
  --exclude coverage.xml
  --exclude .env
  --exclude '*.sqlite'
  --exclude '*.sqlite3'
  --exclude '*.db'
  --exclude '*.pem'
  --exclude '*.key'
)

copy_dir() {
  local rel="$1"
  local dest_root="$2"
  mkdir -p "${dest_root}/${rel}"
  rsync "${rsync_common[@]}" "${ROOT_DIR}/${rel}/" "${dest_root}/${rel}/"
}

copy_file() {
  local rel="$1"
  local dest_root="$2"
  if [[ -f "${ROOT_DIR}/${rel}" ]]; then
    mkdir -p "$(dirname "${dest_root}/${rel}")"
    rsync "${rsync_common[@]}" "${ROOT_DIR}/${rel}" "${dest_root}/${rel}"
  fi
}

zip_root() {
  local root_name="$1"
  local zip_path="$2"
  (
    cd "$WORK_DIR"
    zip -qr -X "$zip_path" "$root_name"
  )
}

copy_common_app_scaffold() {
  local dest_root="$1"
  copy_file AGENTS.md "$dest_root"
  copy_file README.md "$dest_root"
  copy_file apps/README.md "$dest_root"
  copy_file apps/RELEASE_CHECKLIST.md "$dest_root"
  copy_dir apps/shared "$dest_root"
}

mkdir -p "$OUT_DIR"
rm -rf "$WORK_DIR" "$PACKAGE_DIR" "${OUT_DIR}/${SLUG}-package.zip"
mkdir -p "$WORK_DIR" "$PACKAGE_DIR"

full_root="${WORK_DIR}/${SLUG}-full"
mkdir -p "$full_root"
rsync "${rsync_common[@]}" "${ROOT_DIR}/" "$full_root/"

core_root="${WORK_DIR}/Omni-desk-AI-${NATIVE_VERSION}-core-release"
mkdir -p "$core_root"
rsync "${rsync_common[@]}" \
  --exclude apps/web-admin-next \
  --exclude apps/desktop-tauri \
  --exclude apps/mobile-flutter \
  "${ROOT_DIR}/" "$core_root/"

web_root="${WORK_DIR}/Omni-desk-AI-${NATIVE_VERSION}-web-admin"
mkdir -p "$web_root"
copy_common_app_scaffold "$web_root"
copy_dir apps/web-admin-next "$web_root"

desktop_root="${WORK_DIR}/Omni-desk-AI-${NATIVE_VERSION}-desktop"
mkdir -p "$desktop_root"
copy_common_app_scaffold "$desktop_root"
copy_dir apps/desktop-tauri "$desktop_root"

mobile_root="${WORK_DIR}/Omni-desk-AI-${NATIVE_VERSION}-mobile"
mkdir -p "$mobile_root"
copy_common_app_scaffold "$mobile_root"
copy_dir apps/mobile-flutter "$mobile_root"

zip_root "Omni-desk-AI-${NATIVE_VERSION}-core-release" "${PACKAGE_DIR}/Omni-desk-AI-${NATIVE_VERSION}-core-release.zip"
zip_root "Omni-desk-AI-${NATIVE_VERSION}-web-admin" "${PACKAGE_DIR}/Omni-desk-AI-${NATIVE_VERSION}-web-admin.zip"
zip_root "Omni-desk-AI-${NATIVE_VERSION}-desktop" "${PACKAGE_DIR}/Omni-desk-AI-${NATIVE_VERSION}-desktop.zip"
zip_root "Omni-desk-AI-${NATIVE_VERSION}-mobile" "${PACKAGE_DIR}/Omni-desk-AI-${NATIVE_VERSION}-mobile.zip"
zip_root "${SLUG}-full" "${PACKAGE_DIR}/${SLUG}-full.zip"

printf '%s\n' "$SOURCE_COMMIT" > "${PACKAGE_DIR}/SOURCE_COMMIT.txt"
cp "${ROOT_DIR}/docs/SOURCE_GATED_PRODUCTION_GA_CANDIDATE_1.12.7.md" "${PACKAGE_DIR}/README.md"

python3 "${ROOT_DIR}/scripts/write_portable_sha256s.py" \
  --base-dir "$PACKAGE_DIR" \
  --output SHA256SUMS.txt \
  "Omni-desk-AI-${NATIVE_VERSION}-core-release.zip" \
  "Omni-desk-AI-${NATIVE_VERSION}-web-admin.zip" \
  "Omni-desk-AI-${NATIVE_VERSION}-desktop.zip" \
  "Omni-desk-AI-${NATIVE_VERSION}-mobile.zip" \
  "${SLUG}-full.zip"
python3 "${ROOT_DIR}/scripts/write_portable_sha256s.py" --base-dir "$PACKAGE_DIR" --output SHA256SUMS.txt --verify
python3 "${ROOT_DIR}/scripts/write_distribution_manifest.py" \
  --package-dir "$PACKAGE_DIR" \
  --version "$VERSION" \
  --package-slug "$SLUG" \
  --source-commit "$SOURCE_COMMIT" \
  --external-audit "${ROOT_DIR}/release/real-ga-evidence-audit-1.12.7.json" \
  --output release-manifest.json
python3 "${ROOT_DIR}/scripts/write_real_ga_evidence_summary.py" "$ROOT_DIR" \
  --audit-report "${ROOT_DIR}/release/real-ga-evidence-audit-1.12.7.json" \
  --output "${PACKAGE_DIR}/real-ga-evidence-summary.json" \
  --source-commit "$SOURCE_COMMIT"
python3 "${ROOT_DIR}/scripts/write_distribution_manifest.py" --package-dir "$PACKAGE_DIR" --verify --manifest release-manifest.json

(
  cd "$OUT_DIR"
  zip -qr -X "${SLUG}-package.zip" "$SLUG"
)

python3 "${ROOT_DIR}/scripts/check_release_hygiene.py" "$PACKAGE_DIR"
unzip -tq "${OUT_DIR}/${SLUG}-package.zip" >/dev/null
rm -rf "$WORK_DIR"

echo "Distribution package directory: ${PACKAGE_DIR}"
echo "Distribution wrapper zip: ${OUT_DIR}/${SLUG}-package.zip"
