#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import sys
from contextlib import closing
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def quick_check(path: Path) -> str:
    with closing(sqlite3.connect(path)) as con:
        return str(con.execute("PRAGMA quick_check").fetchone()[0])


def _fernet_from_env(key_env: str) -> Fernet:
    secret = os.getenv(key_env, "")
    if len(secret) < 32:
        raise RuntimeError(f"backup encryption key must be at least 32 chars: {key_env}")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def _quick_check_item(path: Path, item: dict, key_env: str) -> str:
    if not item.get("encrypted"):
        return quick_check(path)
    plaintext = _fernet_from_env(key_env).decrypt(path.read_bytes())
    if item.get("plaintext_sha256") and hashlib.sha256(plaintext).hexdigest() != item.get("plaintext_sha256"):
        return "plaintext sha256 mismatch"
    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as tmp:
        tmp.write(plaintext)
        tmp.flush()
        return quick_check(Path(tmp.name))



def _verify_manifest_hmac(manifest: dict, key_env: str) -> bool:
    expected = manifest.get("manifest_hmac_sha256")
    if not expected:
        return False
    secret = os.getenv(key_env or manifest.get("manifest_hmac_key_env", "OMNIDESK_BACKUP_MANIFEST_KEY"), "")
    if len(secret) < 32:
        return False
    unsigned = {k: v for k, v in manifest.items() if k != "manifest_hmac_sha256"}
    payload = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    actual = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(actual, str(expected))

def _verify_postgres(args: argparse.Namespace, manifest: dict) -> list[dict]:
    """Checksums, decryption and table of contents; optionally a full trial restore."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from omnidesk_agent.storage.postgres_backup import BackupError, restore_backup, verify_backup_set

    secret = os.getenv(args.key_env) or None
    signing = os.getenv(args.manifest_key_env) or None
    failures: list[dict] = []
    if args.require_encryption and not all(entry.get("encrypted") for entry in manifest.get("databases", [])):
        failures.append({"manifest": str(args.manifest), "error": "backup is not encrypted"})
    try:
        verify_backup_set(args.manifest, encryption_secret=secret, signing_secret=signing, require_signature=args.require_manifest_signature)
    except BackupError as exc:
        return failures + [{"manifest": str(args.manifest), "error": str(exc)}]
    if args.restore_into_dsn_env:
        target = os.getenv(args.restore_into_dsn_env, "")
        if not target or not args.confirm_database:
            return failures + [{"error": "--restore-into-dsn-env needs a set DSN variable and --confirm-database"}]
        for entry in manifest.get("databases", []):
            if args.database and entry.get("name") not in args.database:
                continue
            try:
                restore_backup(args.manifest, database=str(entry["name"]), target_dsn=target, confirm_database=args.confirm_database, clean=True, encryption_secret=secret, signing_secret=signing, require_signature=args.require_manifest_signature)
            except BackupError as exc:
                failures.append({"database": entry.get("name"), "error": str(exc)})
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify SQLite or PostgreSQL backup sets: checksums, decryption, and integrity.")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--require-encryption", action="store_true")
    parser.add_argument("--key-env", default="OMNIDESK_BACKUP_ENCRYPTION_KEY")
    parser.add_argument("--require-manifest-signature", action="store_true")
    parser.add_argument("--manifest-key-env", default="OMNIDESK_BACKUP_MANIFEST_KEY")
    parser.add_argument("--restore-into-dsn-env", help="PostgreSQL only: trial-restore every database into this scratch database (dropped objects first) and compare row counts.")
    parser.add_argument("--confirm-database", help="Name of the scratch database for --restore-into-dsn-env.")
    parser.add_argument("--database", action="append", help="PostgreSQL only: limit the trial restore to these databases.")
    parser.add_argument("--metrics-file", type=Path, help="node-exporter textfile that records failures for the OmniDeskBackupVerifyFailure alert.")
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest.get("format") == "omnidesk-postgres-backup/v1":
        failures = _verify_postgres(args, manifest)
        _record(args.metrics_file, kind="postgres", ok=not failures)
        print(json.dumps({"ok": not failures, "failures": failures}, sort_keys=True))
        return 0 if not failures else 1
    failures = []
    if args.require_manifest_signature and not _verify_manifest_hmac(manifest, args.manifest_key_env):
        failures.append({"manifest": str(args.manifest), "error": "manifest hmac signature missing or invalid"})
    for item in manifest.get("items", []):
        if not item.get("ok"):
            failures.append({"item": item, "error": "backup item was not ok"})
            continue
        path = Path(item["backup"])
        if not path.exists():
            failures.append({"backup": str(path), "error": "missing"})
            continue
        if sha256(path) != item.get("sha256"):
            failures.append({"backup": str(path), "error": "sha256 mismatch"})
            continue
        if args.require_encryption and not item.get("encrypted"):
            failures.append({"backup": str(path), "error": "backup is not encrypted"})
            continue
        try:
            qc = _quick_check_item(path, item, args.key_env)
        except Exception as exc:
            failures.append({"backup": str(path), "error": str(exc)})
            continue
        if qc.lower() != "ok":
            failures.append({"backup": str(path), "error": f"quick_check={qc}"})
    _record(args.metrics_file, kind="sqlite", ok=not failures)
    print(json.dumps({"ok": not failures, "failures": failures}, sort_keys=True))
    return 0 if not failures else 1


def _record(metrics_file: Path | None, *, kind: str, ok: bool) -> None:
    if metrics_file is None:
        return
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from omnidesk_agent.storage.postgres_backup import record_verification

    record_verification(metrics_file, kind=kind, ok=ok)


if __name__ == "__main__":
    raise SystemExit(main())
