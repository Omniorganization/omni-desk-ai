from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
import time
from pathlib import Path

from scripts import backup_sqlite, restore_sqlite, verify_backup


def test_encrypted_backup_verify_restore_and_retention(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_BACKUP_ENCRYPTION_KEY", "k" * 40)
    db = tmp_path / "runtime.sqlite3"
    with closing(sqlite3.connect(db)) as con:
        with con:
            con.execute("CREATE TABLE health(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            con.execute("INSERT INTO health(value) VALUES ('ok')")

    dest = tmp_path / "backup"
    dest.mkdir()
    old = dest / "runtime.sqlite3.1.bak"
    old.write_bytes(b"old")
    old_time = time.time() - 3 * 86400
    os.utime(old, (old_time, old_time))

    assert backup_sqlite.main(["--dest", str(dest), "--encrypt", "--retention-days", "1", str(db)]) == 0
    manifest_path = dest / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    item = manifest["items"][0]
    backup_path = Path(item["backup"])
    assert item["encrypted"] is True
    assert backup_path.suffix == ".enc"
    assert backup_path.exists()
    assert not old.exists()

    assert verify_backup.main([str(manifest_path), "--require-encryption"]) == 0
    restored = tmp_path / "restored.sqlite3"
    assert restore_sqlite.main([str(backup_path), str(restored), "--force", "--encrypted"]) == 0
    with closing(sqlite3.connect(restored)) as con:
        assert con.execute("SELECT value FROM health").fetchone()[0] == "ok"


def test_verify_backup_requires_encryption(tmp_path: Path):
    db = tmp_path / "runtime.sqlite3"
    with closing(sqlite3.connect(db)) as con:
        with con:
            con.execute("CREATE TABLE health(value TEXT)")
    dest = tmp_path / "backup"
    assert backup_sqlite.main(["--dest", str(dest), str(db)]) == 0
    assert verify_backup.main([str(dest / "backup_manifest.json"), "--require-encryption"]) == 1
