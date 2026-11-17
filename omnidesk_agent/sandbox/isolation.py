from __future__ import annotations

import os
import unicodedata
from pathlib import Path, PureWindowsPath
from typing import Mapping, Optional, Sequence

# Docker/Podman parse a --mount value as one CSV record of key=value fields,
# so these characters in a host path could add fields (a second src=, rw,
# bind-propagation, ...) instead of naming a directory.
_MOUNT_SPEC_CHARS = frozenset(",=\"'")


def bind_mount_source(path: Path | str) -> str:
    """Return ``path`` for the ``src=`` field of a ``--mount type=bind`` value.

    Refuses commas, ``=``, quotes and control characters rather than trying to
    quote them: the path is meant to be a plain directory name.
    """
    text = str(path)
    if not text or any(ch in _MOUNT_SPEC_CHARS or unicodedata.category(ch) == "Cc" for ch in text):
        raise ValueError("sandbox workspace path contains characters that are not allowed in a --mount source (comma, '=', quotes or control characters)")
    return text


# What locating and running the allowlisted tools needs. Workspace code
# (conftest.py, setup.py, ...) runs with this environment, so the gateway's
# tokens, keys and service settings are never passed through.
_HOST_ENV_KEYS = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "TMPDIR", "TEMP", "TMP", "VIRTUAL_ENV", "PYTHONPATH",
    # Non-secret tool settings: CA bundles, git identity, proxy exclusions.
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "PIP_CERT",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    "NO_PROXY", "no_proxy",
    # Windows needs these to start Python, resolve executables and find the
    # user's home and config directories.
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
)
# git subcommands that may authenticate to a remote over SSH or sign with an
# SSH key held by the agent. Repository hooks are disabled at execution time.
_GIT_SSH_SUBCOMMANDS = frozenset({"pull", "push", "commit"})
_GIT_SSH_ENV_KEYS = ("SSH_AUTH_SOCK", "GIT_SSH", "GIT_SSH_COMMAND")
# Proxy URLs can embed credentials: only commands that download or push get them.
_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _program(arg: str) -> str:
    # PureWindowsPath splits on both / and backslash: works for either OS's paths.
    name = PureWindowsPath(arg).name.lower()
    return name[:-4] if name.endswith(".exe") else name


def _is_pip_install(args: list[str]) -> bool:
    program = _program(args[0])
    if program in {"pip", "pip3"}:
        return args[1:2] == ["install"]
    return program.startswith("python") and args[1:4] == ["-m", "pip", "install"]


def minimal_host_env(argv: Sequence[str], environ: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Environment for a command the host argv backend runs in the workspace."""
    source = os.environ if environ is None else environ
    args = [str(arg) for arg in argv]
    keys = list(_HOST_ENV_KEYS)
    git_subcommand = args[1] if len(args) > 1 and _program(args[0]) == "git" else ""
    if git_subcommand in _GIT_SSH_SUBCOMMANDS:
        keys.extend(_GIT_SSH_ENV_KEYS)
    if git_subcommand in {"pull", "push"} or (args and _is_pip_install(args)):
        keys.extend(_PROXY_ENV_KEYS)
    env: dict[str, str] = {}
    for key in keys:
        # Windows environment names are case-insensitive: keep one spelling.
        if key in source and not (os.name == "nt" and key.upper() in {k.upper() for k in env}):
            env[key] = source[key]
    return env
