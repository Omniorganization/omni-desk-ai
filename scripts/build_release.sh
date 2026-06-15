#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

rm -rf dist build ./*.egg-info __MACOSX
find . -name ".DS_Store" -delete
find . -type d -name "__pycache__" -prune -exec rm -rf {} +
find . -type d -name ".pytest_cache" -prune -exec rm -rf {} +
find . -type d -name ".ruff_cache" -prune -exec rm -rf {} +
find . -type d -name ".mypy_cache" -prune -exec rm -rf {} +
find . -type f \( -name "*.pyc" -o -name "*.pyo" -o -name ".coverage" -o -name "coverage.json" -o -name "coverage.xml" \) -delete
chmod +x scripts/*.sh
python scripts/check_script_executability.py .
python scripts/check_release_hygiene.py . --allow-vcs

python - <<'PY_RELEASE_PREFLIGHT'
from __future__ import annotations
import importlib.util
import sys

missing = [name for name in ["build"] if importlib.util.find_spec(name) is None]
if missing:
    print("Missing release build dependencies: " + ", ".join(missing), file=sys.stderr)
    print("Install with: python -m pip install --require-hashes -r requirements.dev.lock && python -m pip install -e . --no-deps --no-build-isolation", file=sys.stderr)
    raise SystemExit(2)
PY_RELEASE_PREFLIGHT

python -m build --no-isolation

python - <<'PY_RELEASE_META'
from __future__ import annotations
import hashlib
import json
from pathlib import Path

root = Path.cwd()
try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
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

PY_RELEASE_META

if [[ -z "${OMNIDESK_IMAGE_DIGEST:-}" ]]; then
  echo "OMNIDESK_IMAGE_DIGEST is required; release workflow must build/push the image and export the registry digest before metadata generation" >&2
  exit 3
fi
python scripts/write_release_metadata.py dist --build-sha "${GITHUB_SHA:-${OMNIDESK_BUILD_SHA:-unknown}}" --image-ref "${OMNIDESK_IMAGE_REF:-}" --image-digest "${OMNIDESK_IMAGE_DIGEST}"

python - <<'PY_RELEASE_CHECKSUMS'
from __future__ import annotations
import hashlib
from pathlib import Path
root = Path.cwd()
checksums = []
for artifact in sorted((root / "dist").iterdir()):
    if artifact.is_file() and artifact.name != "checksums.txt":
        checksums.append(f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}")
(root / "dist" / "checksums.txt").write_text("\n".join(checksums) + "\n", encoding="utf-8")
PY_RELEASE_CHECKSUMS

if [[ "${OMNIDESK_RELEASE_SMOKE:-1}" == "1" ]]; then
  bash scripts/release_smoke_locked.sh
fi

if [[ -z "${OMNIDESK_RELEASE_SIGNING_KEY:-}" && "${OMNIDESK_ALLOW_UNSIGNED_RELEASE:-0}" != "1" ]]; then
  echo "OMNIDESK_RELEASE_SIGNING_KEY is required for production release artifacts" >&2
  exit 2
fi
if [[ -n "${OMNIDESK_RELEASE_SIGNING_KEY:-}" ]]; then
  python scripts/sign_release.py dist
  python scripts/verify_release_signatures.py dist
  python scripts/verify_release_artifact.py dist --require-signatures --require-metadata
else
  echo "Unsigned release allowed only because OMNIDESK_ALLOW_UNSIGNED_RELEASE=1" >&2
  python scripts/verify_release_artifact.py dist --require-metadata
fi

echo "Release artifacts written to dist/:"
ls -lh dist
