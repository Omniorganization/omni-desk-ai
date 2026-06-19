#!/usr/bin/env bash
set -euo pipefail
ROOT="${1:-.}"
OUT="${2:-dist/omni-clean.zip}"
mkdir -p "$(dirname "$OUT")"
PYTHON_BIN="${PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi
if [[ -z "$PYTHON_BIN" ]]; then
  echo "python3.11, python3, or python is required to package a clean zip" >&2
  exit 127
fi
"$PYTHON_BIN" - "$ROOT" "$OUT" <<'PYZIP'
from pathlib import Path
import stat
import sys
import zipfile

root = Path(sys.argv[1]).resolve(); out = Path(sys.argv[2]).resolve()
blocked_parts = {
    '.git',
    '.venv',
    '.next',
    '.npm-cache',
    '.pytest_cache',
    '.ruff_cache',
    '.mypy_cache',
    '.serena',
    '.dart_tool',
    '__pycache__',
    '__MACOSX',
    'node_modules',
    'build',
    'dist',
    'target',
    'coverage',
    'htmlcov',
}
blocked_suffixes = {'.pyc', '.pyo', '.tsbuildinfo'}
blocked_files = {'.DS_Store', '.coverage', 'coverage.json', 'coverage.xml', '.env', 'npm-debug.log', 'yarn-error.log'}
runtime_suffixes = {'.sqlite', '.sqlite3', '.db', '.pem', '.key'}
runtime_files = {'audit.log', 'gmail_token.json', 'oauth_token.json', 'access_token.json', 'refresh_token.json'}


def allowed(path: Path) -> bool:
    rel = path.relative_to(root)
    if path == out or out in path.parents:
        return False
    if set(rel.parts) & blocked_parts:
        return False
    if rel.suffix in blocked_suffixes or rel.suffix.lower() in runtime_suffixes:
        return False
    if rel.name in blocked_files or rel.name.lower() in runtime_files:
        return False
    if any(part.endswith('.egg-info') for part in rel.parts):
        return False
    return True


with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
    for path in sorted(root.rglob('*')):
        if not path.is_file() or not allowed(path):
            continue
        rel = path.relative_to(root).as_posix()
        info = zipfile.ZipInfo(rel)
        info.date_time = (1980, 1, 1, 0, 0, 0)
        mode = stat.S_IMODE(path.stat().st_mode)
        info.external_attr = (mode & 0xFFFF) << 16
        zf.writestr(info, path.read_bytes())
PYZIP
