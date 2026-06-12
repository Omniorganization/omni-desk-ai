#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

rm -rf dist build ./*.egg-info
find . -type d -name "__pycache__" -prune -exec rm -rf {} +
find . -type f \( -name "*.pyc" -o -name "*.pyo" -o -name ".coverage" -o -name "coverage.json" -o -name "coverage.xml" \) -delete
python scripts/check_release_hygiene.py .
python -m build

python - <<'PY_RELEASE_META'
from __future__ import annotations
import hashlib
import json
from pathlib import Path

root = Path.cwd()
try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - Python 3.9/3.10 fallback
    try:
        import tomli as tomllib
    except ModuleNotFoundError as exc:
        raise SystemExit("Install tomli on Python <3.11 to generate release metadata") from exc

pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
project = pyproject.get("project", {})
package_files = sorted(
    str(path.relative_to(root))
    for path in (root / "omnidesk_agent").rglob("*.py")
)
sbom = {
    "name": project.get("name"),
    "version": project.get("version"),
    "dependencies": project.get("dependencies", []),
    "optional_dependencies": project.get("optional-dependencies", {}),
    "package_files": package_files,
}
(root / "dist" / "sbom.json").write_text(json.dumps(sbom, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

checksums = []
for artifact in sorted((root / "dist").iterdir()):
    if artifact.is_file():
        checksums.append(f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}")
(root / "dist" / "checksums.txt").write_text("\n".join(checksums) + "\n", encoding="utf-8")
PY_RELEASE_META

if [[ "${OMNIDESK_RELEASE_SMOKE:-1}" == "1" ]]; then
  scripts/release_smoke_locked.sh
fi

if [[ -z "${OMNIDESK_RELEASE_SIGNING_KEY:-}" && "${OMNIDESK_ALLOW_UNSIGNED_RELEASE:-0}" != "1" ]]; then
  echo "OMNIDESK_RELEASE_SIGNING_KEY is required for production release artifacts" >&2
  exit 2
fi
if [[ -n "${OMNIDESK_RELEASE_SIGNING_KEY:-}" ]]; then
  python scripts/sign_release.py dist
  python scripts/verify_release_signatures.py dist
else
  echo "Unsigned release allowed only because OMNIDESK_ALLOW_UNSIGNED_RELEASE=1" >&2
fi

echo "Release artifacts written to dist/:"
ls -lh dist
