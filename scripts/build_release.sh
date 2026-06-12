#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

rm -rf dist build ./*.egg-info
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

if [[ -n "${OMNIDESK_RELEASE_SIGNING_KEY:-}" ]]; then
  python scripts/sign_release.py dist
else
  echo "OMNIDESK_RELEASE_SIGNING_KEY not set; release artifacts are checksummed but not signed."
fi

echo "Release artifacts written to dist/:"
ls -lh dist
