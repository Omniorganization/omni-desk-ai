from __future__ import annotations

import pytest
from omnidesk_agent.config import _safe_yaml_load, load_config


def test_safe_yaml_load_does_not_recurse():
    data = _safe_yaml_load("gateway:\n  port: 19999\n")
    assert data["gateway"]["port"] == 19999


def test_load_config_rejects_non_mapping(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("- not\n- mapping\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(p)
