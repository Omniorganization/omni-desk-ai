#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import closing
import time
from pathlib import Path

from cryptography.fernet import Fernet


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fernet_from_env(key_env: str) -> Fernet:
    secret = os.getenv(key_env, "")
    if len(secret) < 32:
        raise RuntimeError(f"backup encryption key must be at least 32 chars: {key_env}")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def _encrypt_backup(path: Path, key_env: str) -> tuple[Path, str]:
    plaintext_sha = sha256(path)
    encrypted = path.with_suffix(path.suffix + ".enc")
    encrypted.write_bytes(_fernet_from_env(key_env).encrypt(path.read_bytes()))
    path.unlink()
    return encrypted, plaintext_sha


def backup_one(src: Path, dest_dir: Path, *, encrypt: bool = False, key_env: str = "OMNIDESK_BACKUP_ENCRYPTION_KEY") -> dict:
    src = src.expanduser().resolve()
    if not src.exists():
        return {"source": str(src), "ok": False, "error": "missing"}
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{src.name}.{int(time.time())}.bak"
    with closing(sqlite3.connect(src)) as source, closing(sqlite3.connect(dest)) as target:
        source.backup(target)
    plaintext_sha = sha256(dest)
    if encrypt:
        dest, plaintext_sha = _encrypt_backup(dest, key_env)
    digest = sha256(dest)
    return {
        "source": str(src),
        "backup": str(dest),
        "ok": True,
        "sha256": digest,
        "plaintext_sha256": plaintext_sha,
        "bytes": dest.stat().st_size,
        "encrypted": bool(encrypt),
        "key_env": key_env if encrypt else "",
    }


def apply_retention(dest_dir: Path, retention_days: float) -> list[str]:
    if retention_days <= 0:
        return []
    cutoff = time.time() - retention_days * 86400
    purged: list[str] = []
    for path in sorted(dest_dir.glob("*.bak*")):
        if path.stat().st_mtime >= cutoff:
            continue
        path.unlink()
        purged.append(str(path))
    return purged



def _manifest_hmac(manifest: dict, key_env: str) -> str:
    secret = os.getenv(key_env, "")
    if len(secret) < 32:
        raise RuntimeError(f"backup manifest signing key must be at least 32 chars: {key_env}")
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

def _redact_manifest_paths(manifest: dict) -> dict:
    redacted = json.loads(json.dumps(manifest))
    for item in redacted.get("items", []):
        if "source" in item:
            item["source_name"] = Path(item["source"]).name
            item["source"] = "<redacted>"
        if "backup" in item:
            item["backup_name"] = Path(item["backup"]).name
    return redacted

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create consistent SQLite backups using sqlite3 backup API.")
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--encrypt", action="store_true", help="Encrypt backup files with a key derived from --key-env.")
    parser.add_argument("--key-env", default="OMNIDESK_BACKUP_ENCRYPTION_KEY")
    parser.add_argument("--retention-days", type=float, default=0, help="Delete *.bak and *.bak.enc files older than this many days.")
    parser.add_argument("--sign-manifest", action="store_true", help="HMAC-sign backup_manifest.json with --manifest-key-env.")
    parser.add_argument("--manifest-key-env", default="OMNIDESK_BACKUP_MANIFEST_KEY")
    parser.add_argument("--redact-manifest-paths", action="store_true", help="Also write backup_manifest.public.json with absolute paths redacted.")
    parser.add_argument("db", nargs="+", type=Path)
    args = parser.parse_args(argv)
    manifest = {
        "created_at": time.time(),
        "encrypted": bool(args.encrypt),
        "retention_days": args.retention_days,
        "items": [backup_one(db, args.dest, encrypt=args.encrypt, key_env=args.key_env) for db in args.db],
        "retention_purged": apply_retention(args.dest, args.retention_days),
    }
    if args.sign_manifest:
        manifest["manifest_hmac_key_env"] = args.manifest_key_env
        manifest["manifest_hmac_sha256"] = _manifest_hmac({k: v for k, v in manifest.items() if k != "manifest_hmac_sha256"}, args.manifest_key_env)
    manifest_path = args.dest / "backup_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    if args.redact_manifest_paths:
        (args.dest / "backup_manifest.public.json").write_text(json.dumps(_redact_manifest_paths(manifest), indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(manifest, sort_keys=True))
    return 0 if all(item.get("ok") for item in manifest["items"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
