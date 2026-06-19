from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_MAX_ARCHIVE_FILES = 512
DEFAULT_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_FILE_BYTES = 1024 * 1024


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class SandboxArchiveLimits:
    max_files: int = DEFAULT_MAX_ARCHIVE_FILES
    max_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES
    max_file_bytes: int = DEFAULT_MAX_ARCHIVE_FILE_BYTES

    @classmethod
    def from_env(cls, *, client: bool = False) -> "SandboxArchiveLimits":
        # Client values default to the same server-side names so local packaging
        # fails before the runner rejects the request. Legacy client-specific envs
        # are still supported but no longer broaden the default limits.
        file_env = "OMNIDESK_SANDBOX_CLIENT_MAX_FILES" if client and os.getenv("OMNIDESK_SANDBOX_CLIENT_MAX_FILES") else "OMNIDESK_SANDBOX_MAX_ARCHIVE_FILES"
        bytes_env = "OMNIDESK_SANDBOX_CLIENT_MAX_BYTES" if client and os.getenv("OMNIDESK_SANDBOX_CLIENT_MAX_BYTES") else "OMNIDESK_SANDBOX_MAX_ARCHIVE_BYTES"
        per_file_env = "OMNIDESK_SANDBOX_CLIENT_MAX_FILE_BYTES" if client and os.getenv("OMNIDESK_SANDBOX_CLIENT_MAX_FILE_BYTES") else "OMNIDESK_SANDBOX_MAX_ARCHIVE_FILE_BYTES"
        return cls(
            max_files=_env_int(file_env, DEFAULT_MAX_ARCHIVE_FILES),
            max_bytes=_env_int(bytes_env, DEFAULT_MAX_ARCHIVE_BYTES),
            max_file_bytes=_env_int(per_file_env, DEFAULT_MAX_ARCHIVE_FILE_BYTES),
        )
