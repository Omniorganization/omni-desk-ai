from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from scripts import check_config_profiles


def test_config_profiles_validate_current_examples() -> None:
    report = check_config_profiles.validate_root(Path("."))

    assert report["ok"] is True
    assert report["profile_issue_count"] == 0
    assert set(report["profiles"]) == {"local", "staging", "production", "enterprise"}


def test_config_profiles_reject_production_browser_enabled(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copytree("examples", root / "examples")
    production = root / "examples" / "config.production.yaml"
    data = yaml.safe_load(production.read_text(encoding="utf-8"))
    data["capabilities"]["browser"]["enabled"] = True
    production.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")

    report = check_config_profiles.validate_root(root)

    assert report["ok"] is False
    assert any("capabilities.browser.enabled must be False" in issue for issue in report["profiles"]["production"]["issues"])


def test_config_profiles_reject_enterprise_without_break_glass(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copytree("examples", root / "examples")
    enterprise = root / "examples" / "config.enterprise.yaml"
    data = yaml.safe_load(enterprise.read_text(encoding="utf-8"))
    data["permissions"]["break_glass_enabled"] = False
    enterprise.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")

    report = check_config_profiles.validate_root(root)

    assert report["ok"] is False
    assert any("permissions.break_glass_enabled must be True" in issue for issue in report["profiles"]["enterprise"]["issues"])
