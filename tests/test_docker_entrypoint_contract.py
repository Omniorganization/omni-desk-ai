from __future__ import annotations

from pathlib import Path


PRODUCTION_CONFIG_PATH = "/data/config.production.yaml"


def test_dockerfile_uses_cli_global_config_before_subcommand():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert f'CMD ["omnidesk", "--config", "{PRODUCTION_CONFIG_PATH}", "serve", "--host", "0.0.0.0"]' in dockerfile
    assert 'CMD ["omnidesk", "serve", "--config",' not in dockerfile
    assert "/data/config.yaml" not in dockerfile


def test_dockerfile_installs_wheel_not_editable_runtime_package():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "python -m build --wheel" in dockerfile
    assert "pip install --no-cache-dir --require-hashes -r /tmp/requirements.bootstrap.lock" in dockerfile
    assert "pip install --no-cache-dir --require-hashes -r /tmp/requirements.runtime.lock" in dockerfile
    assert "pip install --no-cache-dir --no-deps /tmp/*.whl" in dockerfile
    assert "pip install --no-cache-dir -e ." not in dockerfile
    assert "HEALTHCHECK" in dockerfile
