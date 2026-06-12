from __future__ import annotations

import subprocess
from pathlib import Path


def test_dockerignore_excludes_local_and_cache_artifacts():
    patterns = set(Path(".dockerignore").read_text(encoding="utf-8").splitlines())
    for required in {".venv", "__pycache__/", ".pytest_cache/", ".ruff_cache/", ".coverage", "coverage.xml", "*.egg-info/", "__MACOSX/", ".serena/", ".env", ".env.*"}:
        assert required in patterns


def test_release_script_is_shell_valid_and_generates_metadata_contract():
    script = Path("scripts/build_release.sh")
    assert script.exists()
    subprocess.run(["bash", "-n", str(script)], check=True)
    text = script.read_text(encoding="utf-8")
    assert "python -m build" in text
    assert "sbom.json" in text
    assert "checksums.txt" in text
