from __future__ import annotations

import base64
import io
import sys
import tarfile
from pathlib import Path

import pytest

from omnidesk_agent.models.schema_retry import validate_json_text
from omnidesk_agent.sandbox.runner_server import RunnerConfig, _workspace_from_payload, _verify_signature
from scripts.production_smoke_test import build_smoke_workspace_archive


def _tar_b64(files: dict[str, bytes]) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_schema_retry_does_not_import_jsonschema_on_module_import(monkeypatch):
    sys.modules.pop("jsonschema", None)
    import omnidesk_agent.models.schema_retry as schema_retry

    assert "jsonschema" not in sys.modules
    validate_json_text('{"kind":"ok"}', {"type": "object", "required": ["kind"]})
    assert "jsonschema" in sys.modules


def test_runner_extracts_workspace_archive_and_blocks_traversal(tmp_path):
    cfg = RunnerConfig(allow_workspace_paths=False)
    workspace, tmp = _workspace_from_payload({"workspace_archive_base64": _tar_b64({"hello.py": b"print('ok')\n"})}, cfg)
    try:
        assert (workspace / "hello.py").read_text() == "print('ok')\n"
    finally:
        assert tmp is not None
        tmp.cleanup()
    with pytest.raises(ValueError, match="unsafe path"):
        _workspace_from_payload({"workspace_archive_base64": _tar_b64({"../escape.py": b"bad"})}, cfg)


def test_runner_path_mode_is_disabled_by_default(tmp_path):
    with pytest.raises(ValueError, match="workspace path payloads are disabled"):
        _workspace_from_payload({"workspace": str(tmp_path)}, RunnerConfig())


def test_runner_requires_hmac_when_configured_for_production(monkeypatch):
    monkeypatch.delenv("OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET", raising=False)
    cfg = RunnerConfig(require_hmac=True)
    ok, reason = _verify_signature({}, b"{}", cfg)
    assert ok is False
    assert "hmac is required" in reason


def test_smoke_workspace_archive_is_real_tar_gz():
    raw = base64.b64decode(build_smoke_workspace_archive())
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
        assert "hello.py" in tf.getnames()


def test_docker_runtime_uses_runtime_lock_and_builder_uses_dev_lock():
    dockerfile = Path("Dockerfile").read_text()
    assert "requirements.runtime.lock" in dockerfile
    assert "requirements.dev.lock" in dockerfile
    assert "--require-hashes -r /tmp/requirements.runtime.lock" in dockerfile
    assert "--no-deps /tmp/*.whl" in dockerfile
