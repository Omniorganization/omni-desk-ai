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
import re

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

lockfiles = [
    "requirements.lock",
    "requirements.bootstrap.lock",
    "requirements.runtime.lock",
    "requirements.enterprise.lock",
    "requirements.security.lock",
    "requirements.dev.lock",
]

def parse_lockfile(path: Path) -> dict:
    entries = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("--"):
            continue
        requirement = line.split(" \\", 1)[0].strip()
        requirement = requirement.split(" --hash=", 1)[0].strip()
        match = re.match(r"([A-Za-z0-9_.-]+)==([^;\s]+)", requirement)
        if match:
            entries.append({"name": match.group(1).lower().replace("_", "-"), "version": match.group(2)})
    return {
        "path": str(path.relative_to(root)),
        "lockfile_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "package_count": len(entries),
        "packages": entries,
    }

sbom = {
    "schema_version": "omnidesk-lockfile-sbom/v1",
    "name": project.get("name"),
    "version": project.get("version"),
    "source": "hash-locked requirements lockfiles",
    "lockfiles": [parse_lockfile(root / rel) for rel in lockfiles],
    "package_files": package_files,
}
(root / "dist" / "sbom.json").write_text(json.dumps(sbom, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

PY_RELEASE_META

if [[ -z "${OMNIDESK_IMAGE_DIGEST:-}" ]]; then
  echo "OMNIDESK_IMAGE_DIGEST is required; release workflow must build/push the image and export the registry digest before metadata generation" >&2
  exit 3
fi
python scripts/write_release_metadata.py dist --build-sha "${GITHUB_SHA:-${OMNIDESK_BUILD_SHA:-unknown}}" --image-ref "${OMNIDESK_IMAGE_REF:-}" --image-digest "${OMNIDESK_IMAGE_DIGEST}"

if [[ "${OMNIDESK_RELEASE_SMOKE:-1}" == "1" ]]; then
  bash scripts/release_smoke_locked.sh
fi

# checksums.txt and the signatures cover every top-level file in dist/, so they are
# written last by scripts/finalize_release_payload.py.
if [[ "${OMNIDESK_RELEASE_DEFER_SIGNING:-0}" == "1" ]]; then
  # release.yml adds the native bundle, locked Helm values, SLSA provenance and
  # Cosign material to dist/ first and then runs finalize_release_payload.py itself.
  echo "Release signing deferred to scripts/finalize_release_payload.py in the release workflow" >&2
elif [[ -n "${OMNIDESK_RELEASE_SIGNING_ED25519_KEY:-}" || "${OMNIDESK_RELEASE_SIGNATURE_ALGORITHM:-}" == "hmac-sha256" ]]; then
  python scripts/finalize_release_payload.py dist
  python scripts/verify_release_artifact.py dist --require-signatures --require-metadata
elif [[ "${OMNIDESK_ALLOW_UNSIGNED_RELEASE:-0}" == "1" ]]; then
  echo "Unsigned release allowed only because OMNIDESK_ALLOW_UNSIGNED_RELEASE=1" >&2
  python scripts/finalize_release_payload.py dist --checksums-only
else
  echo "OMNIDESK_RELEASE_SIGNING_ED25519_KEY is required for production release artifacts" >&2
  exit 2
fi

echo "Release artifacts written to dist/:"
ls -lh dist
