#!/usr/bin/env python3
"""Create a verified, optionally encrypted and signed, PostgreSQL backup set.

Backs up the core database (``OMNIDESK_POSTGRES_DSN``) and the AppSync database
(``OMNIDESK_APPSYNC_POSTGRES_DSN``) by default, once if both point at the same
database. See ``omnidesk_agent/storage/postgres_backup.py`` and
docs/BACKUP_RESTORE.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omnidesk_agent.storage.backup_policy import retention_warning  # noqa: E402
from omnidesk_agent.storage.postgres_backup import BackupError, create_backup, record_verification, resolve_targets, verify_backup_set  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a verified PostgreSQL backup set for OmniDesk.")
    parser.add_argument("--dest", required=True, type=Path, help="Directory that holds backup sets; one subdirectory per run.")
    parser.add_argument("--database", action="append", metavar="NAME=DSN_ENV", help="Database to back up (repeatable). Default: core and appsync DSNs that are set.")
    parser.add_argument("--encrypt", action="store_true", help="Encrypt archives with a key derived from --key-env.")
    parser.add_argument("--key-env", default="OMNIDESK_BACKUP_ENCRYPTION_KEY")
    parser.add_argument("--sign-manifest", action="store_true", help="HMAC-sign the manifest with --manifest-key-env.")
    parser.add_argument("--manifest-key-env", default="OMNIDESK_BACKUP_MANIFEST_KEY")
    parser.add_argument("--keep", type=int, default=0, help="Keep only the newest N backup sets (0 keeps all).")
    parser.add_argument("--retention-days", type=float, default=None, help="Delete backup sets older than this. Without it every set is kept; a scheduled run must pass it (at most 35 days for personal data).")
    parser.add_argument("--no-verify", action="store_true", help="Skip verifying the new set (checksums, decryption, table of contents).")
    parser.add_argument("--metrics-file", type=Path, help="node-exporter textfile to record the verification result in.")
    args = parser.parse_args(argv)
    warning = retention_warning(args.retention_days)
    if warning:
        print(warning, file=sys.stderr)
    try:
        targets = resolve_targets(args.database)
        encryption = os.getenv(args.key_env, "") if args.encrypt else None
        signing = os.getenv(args.manifest_key_env, "") if args.sign_manifest else None
        if args.encrypt and not encryption:
            raise BackupError(f"missing encryption secret: {args.key_env}")
        if args.sign_manifest and not signing:
            raise BackupError(f"missing manifest signing secret: {args.manifest_key_env}")
    except BackupError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    try:
        manifest_path, manifest = create_backup(args.dest, targets=targets, encryption_secret=encryption, signing_secret=signing, keep=args.keep, retention_days=args.retention_days or 0)
        verified = None
        if not args.no_verify:
            verified = verify_backup_set(manifest_path, encryption_secret=encryption, signing_secret=signing, require_signature=bool(signing))
            record_verification(args.metrics_file, kind="postgres", ok=True)
    except BackupError as exc:
        record_verification(args.metrics_file, kind="postgres", ok=False)
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 3
    summary = {
        "ok": True,
        "manifest": str(manifest_path),
        "databases": [{key: entry[key] for key in ("name", "database", "archive", "bytes", "encrypted", "toc_entries", "duration_seconds")} for entry in manifest["databases"]],
        "verified": verified is not None,
        "retention_purged": manifest.get("retention_purged", []),
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
