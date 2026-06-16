from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

from scripts import check_release_hygiene


def test_dockerignore_excludes_local_and_cache_artifacts():
    patterns = set(Path(".dockerignore").read_text(encoding="utf-8").splitlines())
    for required in {".venv", "__pycache__/", ".pytest_cache/", ".ruff_cache/", ".coverage", "coverage.xml", "*.egg-info/", "__MACOSX/", ".serena/", ".env", ".env.*", "node_modules/", ".next/", ".npm-cache/", "*.tsbuildinfo"}:
        assert required in patterns


def test_release_script_is_shell_valid_and_generates_metadata_contract():
    script = Path("scripts/build_release.sh")
    assert script.exists()
    subprocess.run(["bash", "-n", str(script)], check=True)
    text = script.read_text(encoding="utf-8")
    assert "python -m build" in text
    assert "sbom.json" in text
    assert "checksums.txt" in text
    assert "SHA256SUMS.txt" in text


def test_release_hygiene_blocks_frontend_native_and_runtime_artifacts(tmp_path):
    for rel in [
        "apps/web-admin-next/node_modules/pkg/index.js",
        "apps/web-admin-next/.next/build-manifest.json",
        "apps/web-admin-next/.npm-cache/content",
        "apps/web-admin-next/tsconfig.tsbuildinfo",
        "apps/desktop-tauri/src-tauri/target/debug/app",
        "apps/mobile-flutter/.dart_tool/package_config.json",
        "apps/mobile-flutter/build/app.apk",
        "runtime/audit.log",
        "runtime/secret.pem",
    ]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("artifact", encoding="utf-8")

    assert check_release_hygiene.main(str(tmp_path)) == 1


def test_clean_zip_script_is_non_mutating_and_excludes_generated_artifacts(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "keep.txt").write_text("keep", encoding="utf-8")
    for rel in [
        "node_modules/pkg/index.js",
        ".next/build-manifest.json",
        ".venv/bin/python",
        ".pytest_cache/state",
        "__pycache__/mod.pyc",
        "pkg.egg-info/PKG-INFO",
        "tsconfig.tsbuildinfo",
        ".DS_Store",
    ]:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated", encoding="utf-8")

    out = tmp_path / "clean.zip"
    subprocess.run(["bash", "scripts/package_clean_zip.sh", str(root), str(out)], check=True)

    assert (root / "node_modules/pkg/index.js").exists()
    assert (root / "__pycache__/mod.pyc").exists()
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    assert "keep.txt" in names
    assert all("node_modules" not in name for name in names)
    assert all("__pycache__" not in name for name in names)
    assert all(not name.endswith(".tsbuildinfo") for name in names)


def test_ga_release_gate_allows_live_checkout_vcs_hygiene():
    gate = Path("scripts/check_ga_release_gate.py").read_text(encoding="utf-8")
    assert '"--allow-vcs"' in gate
