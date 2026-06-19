from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import tarfile
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnidesk_agent.sandbox.limits import SandboxArchiveLimits


class RemoteSandboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteSandboxResult:
    ok: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    data: dict[str, Any] | None = None


class RemoteSandboxClient:
    """Small stdlib-only client for an isolated sandbox-runner service.

    The app container must not mount /var/run/docker.sock. Production Docker
    deployments should instead call a dedicated runner with no app secrets and
    a narrow RPC API. The runner is expected to expose POST /v1/run.
    """

    DEFAULT_EXCLUDE_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "dist", "build"}

    def __init__(
        self,
        runner_url: str,
        *,
        token_env: str = "OMNIDESK_SANDBOX_RUNNER_TOKEN",
        hmac_secret_env: str = "OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET",
        max_archive_files: int | None = None,
        max_archive_bytes: int | None = None,
        max_file_bytes: int | None = None,
    ):
        normalized_url = runner_url.rstrip("/")
        parsed = urllib.parse.urlparse(normalized_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("remote sandbox runner_url must be an absolute http(s) URL")
        self.runner_url = normalized_url
        self.token_env = token_env
        self.hmac_secret_env = hmac_secret_env
        limits = SandboxArchiveLimits.from_env(client=True)
        self.max_archive_files = int(max_archive_files if max_archive_files is not None else limits.max_files)
        self.max_archive_bytes = int(max_archive_bytes if max_archive_bytes is not None else limits.max_bytes)
        self.max_file_bytes = int(max_file_bytes if max_file_bytes is not None else limits.max_file_bytes)

    def _token(self) -> str:
        token = os.environ.get(self.token_env, "")
        if not token:
            raise RemoteSandboxError(f"remote sandbox token is not configured: {self.token_env}")
        return token

    async def run_command(
        self,
        *,
        argv: list[str],
        workspace: Path,
        timeout_seconds: int,
        readonly: bool = True,
        stdin: bytes | str | None = None,
        purpose: str = "shell",
        image: str | None = None,
    ) -> RemoteSandboxResult:
        payload = {
            "argv": argv,
            "workspace_archive_base64": self._archive_workspace(workspace),
            "timeout_seconds": int(timeout_seconds),
            "readonly": bool(readonly),
            "request_id": str(uuid.uuid4()),
            "nonce": str(uuid.uuid4()),
            "purpose": purpose,
        }
        if image:
            payload["image"] = image
        if stdin is not None:
            raw_stdin = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
            payload["stdin_base64"] = base64.b64encode(raw_stdin).decode("ascii")
        return await asyncio.to_thread(self._post_run, payload, timeout_seconds)

    def _archive_workspace(self, workspace: Path) -> str:
        workspace = workspace.resolve()
        if not workspace.exists():
            raise RemoteSandboxError(f"workspace does not exist: {workspace}")
        buf = io.BytesIO()
        file_count = 0
        total_bytes = 0
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for child, arcname in self._iter_archive_entries(workspace):
                stat = child.stat()
                size = int(stat.st_size)
                if size > self.max_file_bytes:
                    raise RemoteSandboxError(f"workspace file exceeds sandbox upload limit: {arcname}")
                file_count += 1
                if file_count > self.max_archive_files:
                    raise RemoteSandboxError("workspace exceeds sandbox upload file count limit")
                total_bytes += size
                if total_bytes > self.max_archive_bytes:
                    raise RemoteSandboxError("workspace exceeds sandbox upload byte limit")
                tf.add(child, arcname=arcname, recursive=False)
        compressed_size = buf.tell()
        if compressed_size > self.max_archive_bytes:
            raise RemoteSandboxError("compressed workspace archive exceeds sandbox upload byte limit")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def _iter_archive_entries(self, workspace: Path) -> list[tuple[Path, str]]:
        if workspace.is_symlink():
            raise RemoteSandboxError("workspace symlinks are not accepted for remote sandbox upload")
        if workspace.is_file():
            return [(workspace, workspace.name)]
        entries: list[tuple[Path, str]] = []
        for child in sorted(workspace.rglob("*")):
            rel = child.relative_to(workspace)
            if self._is_excluded(rel):
                continue
            if any(part == ".." for part in rel.parts):
                continue
            if child.is_symlink():
                raise RemoteSandboxError(f"workspace symlink is not accepted for remote sandbox upload: {rel}")
            if not child.is_file():
                # Directories, sockets, device nodes and other special files are
                # not needed for command execution and are excluded fail-closed.
                if child.is_dir():
                    continue
                raise RemoteSandboxError(f"unsupported workspace entry for remote sandbox upload: {rel}")
            entries.append((child, str(rel)))
        return entries

    def _is_excluded(self, rel: Path) -> bool:
        return any(part in self.DEFAULT_EXCLUDE_DIRS for part in rel.parts)

    def _post_run(self, payload: dict[str, Any], timeout_seconds: int) -> RemoteSandboxResult:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {self._token()}",
        }
        secret = os.environ.get(self.hmac_secret_env, "")
        if secret:
            ts = str(time.time())
            nonce = str(payload.get("nonce") or uuid.uuid4())
            msg = ts.encode() + b"." + nonce.encode() + b"." + body
            headers["x-omnidesk-sandbox-timestamp"] = ts
            headers["x-omnidesk-sandbox-nonce"] = nonce
            headers["x-omnidesk-sandbox-signature"] = "sha256=" + hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
        req = urllib.request.Request(
            f"{self.runner_url}/v1/run",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            # runner_url is validated during client construction.
            with urllib.request.urlopen(req, timeout=max(1, int(timeout_seconds) + 5)) as resp:  # nosec B310
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RemoteSandboxError(f"remote sandbox HTTP {exc.code}") from exc
        except Exception as exc:
            raise RemoteSandboxError(f"remote sandbox request failed: {exc}") from exc
        return RemoteSandboxResult(
            ok=bool(data.get("ok", False)),
            exit_code=int(data.get("exit_code", 1)),
            stdout=str(data.get("stdout", "")),
            stderr=str(data.get("stderr", "")),
            data=data if isinstance(data, dict) else None,
        )
