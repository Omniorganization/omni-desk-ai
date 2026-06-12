from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import tempfile
import tarfile
import io
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from omnidesk_agent.config import DEFAULT_SANDBOX_IMAGE


ALLOWED_PREFIXES = [
    ["python", "-m", "compileall"],
    ["python3", "-m", "compileall"],
    ["pytest"],
    ["ruff", "check"],
    ["git", "diff"],
    ["git", "status"],
]

_NONCE_LOCK = threading.Lock()
_NONCES: dict[str, float] = {}


@dataclass(frozen=True)
class RunnerConfig:
    token_env: str = "OMNIDESK_SANDBOX_RUNNER_TOKEN"
    hmac_secret_env: str = "OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET"
    image_allowlist_env: str = "OMNIDESK_SANDBOX_IMAGE_ALLOWLIST"
    max_timeout_seconds: int = int(os.getenv("OMNIDESK_SANDBOX_MAX_TIMEOUT", "180"))
    max_output_chars: int = int(os.getenv("OMNIDESK_SANDBOX_MAX_OUTPUT_CHARS", "12000"))
    max_body_bytes: int = int(os.getenv("OMNIDESK_SANDBOX_MAX_BODY_BYTES", str(4 * 1024 * 1024)))
    max_archive_bytes: int = int(os.getenv("OMNIDESK_SANDBOX_MAX_ARCHIVE_BYTES", str(2 * 1024 * 1024)))
    timestamp_skew_seconds: int = int(os.getenv("OMNIDESK_SANDBOX_TIMESTAMP_SKEW_SECONDS", "120"))
    nonce_ttl_seconds: int = int(os.getenv("OMNIDESK_SANDBOX_NONCE_TTL_SECONDS", "300"))
    container_runtime: str = os.getenv("OMNIDESK_CONTAINER_RUNTIME", "docker")
    allowed_workspace_root: Path = Path(os.getenv("OMNIDESK_SANDBOX_ALLOWED_WORKSPACE_ROOT", "/srv/omnidesk-sandbox-workspaces")).resolve()
    allow_workspace_paths: bool = os.getenv("OMNIDESK_SANDBOX_ALLOW_WORKSPACE_PATHS", "0").lower() in {"1", "true", "yes"}
    require_hmac: bool = os.getenv("OMNIDESK_SANDBOX_REQUIRE_HMAC", "1" if os.getenv("OMNIDESK_ENV") == "production" else "0").lower() in {"1", "true", "yes"}
    max_archive_files: int = int(os.getenv("OMNIDESK_SANDBOX_MAX_ARCHIVE_FILES", "512"))
    max_archive_file_bytes: int = int(os.getenv("OMNIDESK_SANDBOX_MAX_ARCHIVE_FILE_BYTES", str(1024 * 1024)))
    default_image: str = os.getenv("OMNIDESK_SANDBOX_IMAGE", DEFAULT_SANDBOX_IMAGE)


def _allowed(argv: list[str]) -> bool:
    return any(len(argv) >= len(prefix) and argv[: len(prefix)] == prefix for prefix in ALLOWED_PREFIXES)


def _constant_time_ok(expected: str, actual: str) -> bool:
    return bool(expected) and hmac.compare_digest(expected.encode(), actual.encode())


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _prune_nonces(now: float, ttl: int) -> None:
    expired = [nonce for nonce, seen_at in _NONCES.items() if now - seen_at > ttl]
    for nonce in expired:
        _NONCES.pop(nonce, None)


def _verify_signature(headers: dict[str, str], body: bytes, cfg: RunnerConfig) -> tuple[bool, str]:
    secret = os.getenv(cfg.hmac_secret_env, "")
    if not secret:
        return (False, "hmac is required") if cfg.require_hmac else (True, "hmac not configured")
    ts = headers.get("x-omnidesk-sandbox-timestamp", "")
    nonce = headers.get("x-omnidesk-sandbox-nonce", "")
    sig = headers.get("x-omnidesk-sandbox-signature", "")
    if not ts or not nonce or not sig:
        return False, "missing signature headers"
    try:
        now = time.time()
        if abs(now - float(ts)) > cfg.timestamp_skew_seconds:
            return False, "stale signature timestamp"
    except ValueError:
        return False, "invalid signature timestamp"
    with _NONCE_LOCK:
        _prune_nonces(now, cfg.nonce_ttl_seconds)
        if nonce in _NONCES:
            return False, "replayed signature nonce"
        _NONCES[nonce] = now
    msg = ts.encode() + b"." + nonce.encode() + b"." + body
    expected = "sha256=" + hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        # Do not keep nonce on bad signature; otherwise a malformed request can burn valid nonces.
        with _NONCE_LOCK:
            _NONCES.pop(nonce, None)
        return False, "invalid signature"
    return True, "ok"


def _image_allowlist(cfg: RunnerConfig) -> set[str]:
    raw = os.getenv(cfg.image_allowlist_env, cfg.default_image)
    return {x.strip() for x in raw.split(",") if x.strip()}


def _build_docker_command(payload: dict[str, Any], workspace: Path, cfg: RunnerConfig) -> list[str]:
    argv = [str(x) for x in payload.get("argv") or []]
    image = str(payload.get("image") or cfg.default_image)
    if image not in _image_allowlist(cfg):
        raise ValueError("sandbox image is not in runner allowlist")
    readonly = bool(payload.get("readonly", True))
    return [
        cfg.container_runtime, "run", "--rm", "--network", "none", "--init", "--pull", "never",
        "--log-driver", "none", "--oom-kill-disable=false", "--memory", "512m", "--cpus", "1.0",
        "--pids-limit", "128", "--user", "65534:65534", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m",
        "--mount", f"type=bind,src={workspace},dst=/workspace,{ 'readonly' if readonly else 'rw' }",
        "-w", "/workspace", "--env", "PYTHONDONTWRITEBYTECODE=1", image, *argv,
    ]


def _safe_extract_workspace_archive(raw: bytes, dest: Path, cfg: RunnerConfig) -> None:
    """Extract a tar/tar.gz workspace with path, symlink, count, and size guards."""
    total = 0
    count = 0
    try:
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:*")
    except tarfile.TarError as exc:
        raise ValueError(f"invalid workspace archive: {exc}") from exc
    with tf:
        members = tf.getmembers()
        if len(members) > cfg.max_archive_files:
            raise ValueError("workspace archive contains too many files")
        for member in members:
            count += 1
            name = member.name
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError("workspace archive contains unsafe path")
            target = (dest / name).resolve()
            if not _is_relative_to(target, dest):
                raise ValueError("workspace archive path escapes destination")
            if member.issym() or member.islnk():
                raise ValueError("workspace archive may not contain links")
            if member.isfile():
                if member.size > cfg.max_archive_file_bytes:
                    raise ValueError("workspace archive file exceeds maximum size")
                total += int(member.size)
                if total > cfg.max_archive_bytes:
                    raise ValueError("workspace archive exceeds maximum size")
            elif not (member.isdir() or member.isfile()):
                raise ValueError("workspace archive contains unsupported entry")
        for member in members:
            target = (dest / member.name).resolve()
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(member)
                if src is None:
                    raise ValueError("workspace archive file could not be read")
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, length=1024 * 1024)


def _workspace_from_payload(payload: dict[str, Any], cfg: RunnerConfig) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    archive_b64 = payload.get("workspace_archive_base64")
    if archive_b64:
        raw = base64.b64decode(str(archive_b64).encode(), validate=True)
        if len(raw) > cfg.max_archive_bytes:
            raise ValueError("workspace archive exceeds maximum size")
        tmp = tempfile.TemporaryDirectory(prefix="omnidesk-sandbox-")
        root = Path(tmp.name).resolve()
        try:
            _safe_extract_workspace_archive(raw, root, cfg)
        except Exception:
            tmp.cleanup()
            raise
        return root, tmp
    if not cfg.allow_workspace_paths:
        raise ValueError("workspace path payloads are disabled; use workspace_archive_base64")
    workspace = Path(str(payload.get("workspace") or cfg.allowed_workspace_root)).resolve()
    if not _is_relative_to(workspace, cfg.allowed_workspace_root):
        raise ValueError("workspace path is outside the allowed sandbox workspace root")
    return workspace, None


def _runtime_ready(cfg: RunnerConfig) -> tuple[bool, str]:
    runtime = shutil.which(cfg.container_runtime)
    if not runtime:
        return False, f"container runtime not found: {cfg.container_runtime}"
    try:
        subprocess.run([cfg.container_runtime, "--version"], text=True, capture_output=True, timeout=5, check=True)
    except Exception as exc:
        return False, f"container runtime unavailable: {exc}"
    if not _image_allowlist(cfg):
        return False, "sandbox image allowlist is empty"
    if os.getenv("OMNIDESK_SANDBOX_READY_SMOKE", "0").lower() in {"1", "true", "yes"}:
        image = cfg.default_image
        if image not in _image_allowlist(cfg):
            return False, "default sandbox image is not allowlisted"
        with tempfile.TemporaryDirectory(prefix="omnidesk-ready-") as tmp:
            payload = {"argv": ["python", "-m", "compileall", "."], "image": image, "readonly": True}
            try:
                subprocess.run(_build_docker_command(payload, Path(tmp).resolve(), cfg), text=True, capture_output=True, timeout=30, check=True)
            except Exception as exc:
                return False, f"sandbox smoke command failed: {exc}"
    return True, "ready"


class SandboxRunnerHandler(BaseHTTPRequestHandler):
    server_version = "OmniDeskSandboxRunner/1.1"

    def _json(self, code: int, data: dict[str, Any]) -> None:
        encoded = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        cfg = self.server.runner_config  # type: ignore[attr-defined]
        if self.path == "/health":
            self._json(200, {"ok": True, "service": "sandbox-runner"})
            return
        if self.path == "/ready":
            ok, reason = _runtime_ready(cfg)
            self._json(200 if ok else 503, {"ok": ok, "service": "sandbox-runner", "reason": reason})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        cfg = self.server.runner_config  # type: ignore[attr-defined]
        if self.path != "/v1/run":
            self._json(404, {"ok": False, "error": "not found"})
            return
        token = os.getenv(cfg.token_env, "")
        auth = self.headers.get("authorization", "")
        if not _constant_time_ok(f"Bearer {token}", auth):
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        length = int(self.headers.get("content-length", "0") or "0")
        if length <= 0 or length > cfg.max_body_bytes:
            self._json(413, {"ok": False, "error": "invalid request body size"})
            return
        body = self.rfile.read(length)
        ok_sig, reason = _verify_signature({k.lower(): v for k, v in self.headers.items()}, body, cfg)
        if not ok_sig:
            self._json(401, {"ok": False, "error": reason})
            return
        try:
            payload = json.loads(body.decode())
            argv = [str(x) for x in payload.get("argv") or []]
            if not argv or not _allowed(argv):
                self._json(400, {"ok": False, "exit_code": 126, "stderr": "command blocked by allowlist"})
                return
            timeout = min(int(payload.get("timeout_seconds") or 120), cfg.max_timeout_seconds)
            workspace, tmp = _workspace_from_payload(payload, cfg)
            try:
                cmd = _build_docker_command(payload, workspace, cfg)
                result = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)
                stdout = result.stdout[-cfg.max_output_chars :]
                stderr = result.stderr[-cfg.max_output_chars :]
                self._json(200, {"ok": result.returncode == 0, "exit_code": result.returncode, "stdout": stdout, "stderr": stderr, "request_id": payload.get("request_id")})
            finally:
                if tmp is not None:
                    tmp.cleanup()
        except subprocess.TimeoutExpired as exc:
            self._json(200, {"ok": False, "exit_code": 124, "stdout": exc.stdout or "", "stderr": "sandbox timed out"})
        except Exception as exc:
            self._json(500, {"ok": False, "exit_code": 1, "stderr": str(exc)})


def run_server(host: str = "0.0.0.0", port: int = 18890, cfg: RunnerConfig | None = None) -> None:
    server = ThreadingHTTPServer((host, port), SandboxRunnerHandler)
    server.runner_config = cfg or RunnerConfig()  # type: ignore[attr-defined]
    server.serve_forever()


if __name__ == "__main__":
    run_server(host=os.getenv("OMNIDESK_SANDBOX_RUNNER_HOST", "0.0.0.0"), port=int(os.getenv("OMNIDESK_SANDBOX_RUNNER_PORT", "18890")))
