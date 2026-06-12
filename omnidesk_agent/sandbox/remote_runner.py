from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import base64
import io
import tarfile
import os
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


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

    def __init__(self, runner_url: str, *, token_env: str = "OMNIDESK_SANDBOX_RUNNER_TOKEN", hmac_secret_env: str = "OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET"):
        self.runner_url = runner_url.rstrip("/")
        self.token_env = token_env
        self.hmac_secret_env = hmac_secret_env

    def _token(self) -> str:
        token = os.environ.get(self.token_env, "")
        if not token:
            raise RemoteSandboxError(f"remote sandbox token is not configured: {self.token_env}")
        return token

    async def run_command(self, *, argv: list[str], workspace: Path, timeout_seconds: int, readonly: bool = True) -> RemoteSandboxResult:
        payload = {
            "argv": argv,
            "workspace_archive_base64": self._archive_workspace(workspace),
            "timeout_seconds": int(timeout_seconds),
            "readonly": bool(readonly),
            "request_id": str(uuid.uuid4()),
            "nonce": str(uuid.uuid4()),
        }
        return await asyncio.to_thread(self._post_run, payload, timeout_seconds)

    def _archive_workspace(self, workspace: Path) -> str:
        workspace = workspace.resolve()
        if not workspace.exists():
            raise RemoteSandboxError(f"workspace does not exist: {workspace}")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            if workspace.is_file():
                tf.add(workspace, arcname=workspace.name, recursive=False)
            else:
                for child in sorted(workspace.rglob("*")):
                    if ".git" in child.parts or "__pycache__" in child.parts:
                        continue
                    rel = child.relative_to(workspace)
                    if any(part == ".." for part in rel.parts):
                        continue
                    tf.add(child, arcname=str(rel), recursive=False)
        return base64.b64encode(buf.getvalue()).decode("ascii")

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
            with urllib.request.urlopen(req, timeout=max(1, int(timeout_seconds) + 5)) as resp:  # nosec - URL is operator-configured
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
