#!/usr/bin/env python3
"""Restore one database of a verified OmniDesk PostgreSQL backup set and prove it.

The target comes from its own environment variable (default
``OMNIDESK_RESTORE_POSTGRES_DSN``) so a restore never defaults to the live
database, and ``--confirm-database`` must name the target database. The target
must be empty unless ``--clean`` is given. After ``pg_restore
--single-transaction`` every table's row count and the AppSync schema versions
are compared with the manifest.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omnidesk_agent.storage.postgres_backup import BackupError, restore_backup  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore and verify an OmniDesk PostgreSQL backup.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--database", required=True, help="Database name in the backup set (core, appsync).")
    parser.add_argument("--target-dsn-env", default="OMNIDESK_RESTORE_POSTGRES_DSN")
    parser.add_argument("--confirm-database", required=True, help="Name of the target database; the restore refuses any other.")
    parser.add_argument("--clean", action="store_true", help="Drop existing objects first (target need not be empty).")
    parser.add_argument("--key-env", default="OMNIDESK_BACKUP_ENCRYPTION_KEY")
    parser.add_argument("--require-signature", action="store_true")
    parser.add_argument("--manifest-key-env", default="OMNIDESK_BACKUP_MANIFEST_KEY")
    parser.add_argument("--report", type=Path, help="Also write the JSON report here (drill evidence).")
    args = parser.parse_args(argv)
    target = os.getenv(args.target_dsn_env, "").strip()
    if not target:
        print(json.dumps({"ok": False, "error": f"missing PostgreSQL DSN environment variable: {args.target_dsn_env}"}), file=sys.stderr)
        return 2
    try:
        report = restore_backup(
            args.manifest, database=args.database, target_dsn=target, confirm_database=args.confirm_database, clean=args.clean,
            encryption_secret=os.getenv(args.key_env) or None, signing_secret=os.getenv(args.manifest_key_env) or None,
            require_signature=args.require_signature,
        )
    except BackupError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 3
    text = json.dumps(report, sort_keys=True)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
