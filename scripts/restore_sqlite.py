#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing, nullcontext
from pathlib import Path

from cryptography.fernet import Fernet


def _fernet_from_env(key_env: str) -> Fernet:
    secret = os.getenv(key_env, "")
    if len(secret) < 32:
        raise RuntimeError(f"backup encryption key must be at least 32 chars: {key_env}")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore a verified SQLite backup with a safety copy of the current DB.")
    parser.add_argument("backup", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--encrypted", action="store_true", help="Decrypt backup using --key-env before restore.")
    parser.add_argument("--key-env", default="OMNIDESK_BACKUP_ENCRYPTION_KEY")
    args = parser.parse_args(argv)
    if not args.force:
        raise SystemExit("Refusing to restore without --force")
    with (tempfile.NamedTemporaryFile(suffix=".sqlite3") if args.encrypted else nullcontext(None)) as tmp:
        backup_path = args.backup
        if args.encrypted:
            plaintext = _fernet_from_env(args.key_env).decrypt(args.backup.read_bytes())
            tmp.write(plaintext)
            tmp.flush()
            backup_path = Path(tmp.name)
        with closing(sqlite3.connect(backup_path)) as con:
            qc = con.execute("PRAGMA quick_check").fetchone()[0]
            if str(qc).lower() != "ok":
                raise SystemExit(f"backup quick_check failed: {qc}")
        args.target.parent.mkdir(parents=True, exist_ok=True)
        if args.target.exists():
            shutil.copy2(args.target, args.target.with_suffix(args.target.suffix + f".pre-restore-{int(time.time())}"))
        shutil.copy2(backup_path, args.target)
    print(f"restored {args.backup} -> {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
