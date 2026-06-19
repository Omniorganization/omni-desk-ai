from __future__ import annotations
from pathlib import Path


def test_release_hygiene_patterns_are_blocked():
    script = Path('scripts/package_clean_zip.sh').read_text(encoding='utf-8')
    for needle in ['.pytest_cache', '__pycache__', '.egg-info', 'node_modules', '.DS_Store', '.next', '.venv', '.npm-cache', '.tsbuildinfo']:
        assert needle in script


def test_clean_zip_script_skips_generated_artifacts_before_packaging():
    script = Path('scripts/package_clean_zip.sh').read_text(encoding='utf-8')
    assert "path.is_file() or not allowed(path)" in script
    assert "part.endswith('.egg-info')" in script
    assert "rel.suffix in blocked_suffixes" in script
    assert "date_time = (1980, 1, 1, 0, 0, 0)" in script
