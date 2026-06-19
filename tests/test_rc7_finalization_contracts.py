from __future__ import annotations

import base64
import io
import sys
import tarfile
from pathlib import Path

import pytest

from omnidesk_agent.models.schema_retry import validate_json_text
from omnidesk_agent.sandbox.runner_server import RunnerConfig, _allowed, _workspace_from_payload, _verify_signature
from scripts import production_smoke_test
from scripts.production_smoke_test import build_smoke_workspace_archive


def _tar_b64(files: dict[str, bytes]) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _tar_b64_symlink(name: str, linkname: str) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name)
        info.type = tarfile.SYMTYPE
        info.linkname = linkname
        tf.addfile(info)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_schema_retry_does_not_import_jsonschema_on_module_import(monkeypatch):
    sys.modules.pop("jsonschema", None)

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


def test_runner_rejects_symlink_and_oversized_archive_entries():
    cfg = RunnerConfig(allow_workspace_paths=False)
    with pytest.raises(ValueError, match="links"):
        _workspace_from_payload({"workspace_archive_base64": _tar_b64_symlink("escape", "/etc/passwd")}, cfg)
    with pytest.raises(ValueError, match="file exceeds maximum size"):
        _workspace_from_payload(
            {"workspace_archive_base64": _tar_b64({"large.py": b"print('too large')\n"})},
            RunnerConfig(allow_workspace_paths=False, max_archive_file_bytes=1),
        )


def test_runner_command_allowlist_blocks_shell_escape():
    assert _allowed(["python", "-m", "compileall", "."])
    assert not _allowed(["bash", "-c", "python -m compileall ."])
    assert not _allowed(["python", "-c", "import os; os.system('id')"])


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


def test_smoke_sandbox_only_skips_app_check(monkeypatch):
    monkeypatch.setattr(production_smoke_test, "check_sandbox", lambda: {"ready": {"ok": True}})

    def fail_app():
        raise AssertionError("app check should not run in sandbox-only mode")

    monkeypatch.setattr(production_smoke_test, "check_app", fail_app)
    assert production_smoke_test.main(["--sandbox-only"]) == 0


def test_smoke_strict_sandbox_only_passes_strict_flag(monkeypatch):
    seen: dict[str, bool] = {}

    def fake_check_sandbox(*, strict=False):
        seen["strict"] = strict
        return {"ready": {"ok": True}, "strict_sandbox": {}}

    monkeypatch.setattr(production_smoke_test, "check_sandbox", fake_check_sandbox)
    assert production_smoke_test.main(["--sandbox-only", "--strict-sandbox"]) == 0
    assert seen["strict"] is True


def test_strict_sandbox_smoke_exercises_rejection_paths(monkeypatch):
    calls: list[tuple[dict, str | None]] = []

    def fake_post_json_response(url, payload, token, hmac_secret, *, nonce=None, timestamp=None):
        assert url == "http://runner/v1/run"
        assert token == "token"
        assert hmac_secret == "secret"
        calls.append((payload, nonce))

        if nonce is not None:
            replay_count = sum(1 for _payload, seen_nonce in calls if seen_nonce == nonce)
            if replay_count == 1:
                return 400, {"ok": False, "stderr": "command blocked by allowlist"}
            return 401, {"ok": False, "error": "replayed signature nonce"}

        if payload["argv"] == ["bash", "-c", "id"]:
            return 400, {"ok": False, "stderr": "command blocked by allowlist"}

        raw = base64.b64decode(payload["workspace_archive_base64"])
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            member = tf.getmembers()[0]
        if member.name.startswith("../"):
            return 500, {"ok": False, "stderr": "workspace archive contains unsafe path"}
        if member.issym():
            return 500, {"ok": False, "stderr": "workspace archive may not contain links"}
        if member.size > 1024 * 1024:
            return 500, {"ok": False, "stderr": "workspace archive file exceeds maximum size"}
        raise AssertionError(f"unexpected strict sandbox payload: {payload}")

    monkeypatch.setattr(production_smoke_test, "post_json_response", fake_post_json_response)

    checks = production_smoke_test.check_strict_sandbox("http://runner", "token", "secret", 3)

    assert set(checks) == {
        "path_traversal_archive",
        "symlink_escape_archive",
        "command_allowlist_reject",
        "nonce_replay_reject",
        "oversized_archive_reject",
    }
    assert len(calls) == 6


def test_makefile_exposes_industrial_test_targets():
    makefile = Path("Makefile").read_text(encoding="utf-8")
    for target in ["test:", "test-strict:", "test-ci:", "init-production-config:", "compose-smoke:", "strict-sandbox-smoke:"]:
        assert target in makefile
    assert "--timeout=60 --timeout-method=thread" in makefile
    assert "--strict-sandbox" in makefile


def test_docker_runtime_uses_runtime_lock_and_builder_uses_dev_lock():
    dockerfile = Path("Dockerfile").read_text()
    assert "requirements.bootstrap.lock" in dockerfile
    assert "requirements.runtime.lock" in dockerfile
    assert "requirements.dev.lock" in dockerfile
    assert "--require-hashes -r /tmp/requirements.runtime.lock" in dockerfile
    assert "--no-deps /tmp/*.whl" in dockerfile


def test_sandbox_runner_compose_defaults_to_archive_workspace_protocol():
    compose = Path("deploy/sandbox-runner/docker-compose.yml").read_text()
    assert 'OMNIDESK_SANDBOX_ALLOW_WORKSPACE_PATHS: "0"' in compose
    assert "OMNIDESK_SANDBOX_NONCE_DB" in compose
    assert "omnidesk-sandbox-workspaces:" not in compose
    assert ":/srv/omnidesk-sandbox-workspaces" not in compose
