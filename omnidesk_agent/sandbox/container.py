"""Run sandboxed commands in containers that cannot outlive their time budget.

Killing the ``docker run`` / ``podman run`` client on a timeout does not stop the
container: it keeps running detached, and its temporary workspace can be deleted
under it. Every sandbox container therefore gets a unique name, and a timeout or
cancellation kills and removes the container by that name before returning.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from dataclasses import dataclass
from typing import Optional, Sequence

CONTAINER_NAME_PREFIX = "omnidesk-sbx"
_KILL_TIMEOUT_SECONDS = 15


def new_container_name(prefix: str = CONTAINER_NAME_PREFIX) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


def with_container_name(argv: Sequence[str], name: str) -> list[str]:
    """Insert ``--name <name>`` right after ``<runtime> run``."""
    argv = list(argv)
    if len(argv) < 2 or argv[1] != "run":
        raise ValueError("expected a '<runtime> run ...' command")
    if "--name" in argv:
        raise ValueError("sandbox container command already names its container")
    return [argv[0], "run", "--name", name, *argv[2:]]


def kill_container(runtime: str, name: str) -> None:
    """Stop and remove a sandbox container; a container that is already gone is fine."""
    for args in (("kill", name), ("rm", "-f", name)):
        try:
            subprocess.run([runtime, *args], capture_output=True, timeout=_KILL_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.SubprocessError):
            continue


@dataclass(frozen=True)
class ContainerResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False

    def __post_init__(self) -> None:
        for name in ("stdout", "stderr"):
            value = getattr(self, name)
            if not isinstance(value, bytes):
                object.__setattr__(self, name, str(value or "").encode("utf-8", errors="replace"))


async def run_container(
    argv: Sequence[str],
    *,
    timeout: float,
    stdin: Optional[bytes] = None,
    name: Optional[str] = None,
) -> ContainerResult:
    """Run a ``<runtime> run`` command; on timeout or cancellation kill the container itself."""
    name = name or new_container_name()
    command = with_container_name(argv, name)
    runtime = command[0]
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        await asyncio.to_thread(kill_container, runtime, name)
        if proc.returncode is None:
            proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=_KILL_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        if isinstance(exc, asyncio.CancelledError):
            raise
        return ContainerResult(124, b"", b"sandbox timed out; container killed", timed_out=True)
    return ContainerResult(int(proc.returncode or 0), stdout, stderr)


def run_container_sync(
    argv: Sequence[str],
    *,
    timeout: float,
    stdin: Optional[bytes] = None,
    name: Optional[str] = None,
) -> ContainerResult:
    """Blocking variant for the sandbox runner service."""
    name = name or new_container_name()
    command = with_container_name(argv, name)
    try:
        result = subprocess.run(command, input=stdin, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        kill_container(command[0], name)
        return ContainerResult(124, exc.stdout or b"", b"sandbox timed out; container killed", timed_out=True)
    return ContainerResult(result.returncode, result.stdout, result.stderr)
