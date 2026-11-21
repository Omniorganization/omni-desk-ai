"""PostgreSQL backup and restore, run for real with pg_dump and pg_restore.

A backup is only worth what a restore proves. These tests back up a live
database while it is being written, restore it into an empty one, and check that
every table's row count and the AppSync schema versions match what the backup
recorded; that tampered, truncated or wrongly keyed archives are refused; that a
restore cannot silently overwrite the wrong or a non-empty database; and that
passwords never appear on a tool's command line.

They need OMNIDESK_TEST_POSTGRES_DSN (a role that may create databases) and the
PostgreSQL client tools (PATH or OMNIDESK_PG_BIN). With a DSN but no tools they
fail instead of skipping, so CI cannot pass them vacuously.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

import pytest

from omnidesk_agent.storage import postgres_backup as pb

SECRET = "backup-encryption-secret-for-tests-0123456789"
SIGNING = "backup-manifest-signing-secret-for-tests-0123"
ROOT = Path(__file__).resolve().parents[1]


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"script_{name}", ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _admin_dsn() -> str:
    value = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "").strip()
    if not value:
        pytest.skip("OMNIDESK_TEST_POSTGRES_DSN is not configured")
    try:
        pb.pg_tool("pg_dump")
        pb.pg_tool("pg_restore")
    except pb.BackupError as exc:
        pytest.fail(f"PostgreSQL client tools are required where a test database is configured: {exc}")
    return value


def _dsn_for(admin: str, database: str) -> str:
    from psycopg import conninfo

    params = conninfo.conninfo_to_dict(admin)
    params["dbname"] = database
    return conninfo.make_conninfo(**params)


@pytest.fixture
def databases() -> Iterator[dict[str, str]]:
    admin = _admin_dsn()  # skips without a test database, before psycopg is needed
    psycopg = pytest.importorskip("psycopg")
    names = {role: f"omnidesk_bk_{role}_{uuid.uuid4().hex[:10]}" for role in ("source", "target", "other")}
    with psycopg.connect(admin, autocommit=True) as conn:
        for name in names.values():
            conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield {role: _dsn_for(admin, name) for role, name in names.items()} | {f"{role}_name": name for role, name in names.items()}
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            for name in names.values():
                conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _populate(dsn: str) -> dict[str, Any]:
    from omnidesk_agent.appsync.migrated_postgres_store import MigratedMultiInstancePostgresAppSyncStore
    from omnidesk_agent.appsync.postgres_migrations import apply_appsync_migrations
    from omnidesk_agent.repositories.postgres_state import PostgresRuntimeStateStores

    apply_appsync_migrations(dsn, namespace="production")
    store = MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace="production", pool_size=2)
    core = PostgresRuntimeStateStores(dsn)
    try:
        store.ensure_user("olivia")
        conversation = store.create_conversation(actor="olivia", title="Quarterly plan")
        for index in range(5):
            store.add_chat_user_message(actor="olivia", conversation_id=conversation["conversation_id"], content=f"note {index}")
        core.state.put("llm_usage", "u1", {"id": "u1", "task_id": "t1", "created_at": time.time()})
        return {"conversation_id": conversation["conversation_id"], "feed": [e["seq"] for e in store.sync_since(0)["events"]]}
    finally:
        store.close()
        core.state.close()


def test_backup_restores_into_an_empty_database_and_proves_it(databases: dict[str, str], tmp_path: Path) -> None:
    source = _populate(databases["source"])
    manifest_path, manifest = pb.create_backup(
        tmp_path / "backups", targets=[pb.Target("appsync", "SRC", databases["source"])],
        encryption_secret=SECRET, signing_secret=SIGNING,
    )
    entry = manifest["databases"][0]
    assert entry["encrypted"] and entry["archive"].endswith(".enc") and entry["toc_entries"] > 10
    assert entry["schema_versions"] == {"production": 6}
    assert entry["row_counts"]["public.omnidesk_appsync_messages"] == 5
    assert not (manifest_path.parent / "appsync.dump").exists(), "the plaintext archive must not be left behind"
    assert pb.verify_backup_set(manifest_path, encryption_secret=SECRET, signing_secret=SIGNING, require_signature=True)["ok"]

    report = pb.restore_backup(
        manifest_path, database="appsync", target_dsn=databases["target"], confirm_database=databases["target_name"],
        encryption_secret=SECRET, signing_secret=SIGNING, require_signature=True,
    )
    assert report["ok"] and report["rows_verified"] == sum(entry["row_counts"].values())
    assert report["restore_seconds"] >= 0 and report["backup_age_seconds"] >= 0

    from omnidesk_agent.appsync.migrated_postgres_store import MigratedMultiInstancePostgresAppSyncStore

    restored = MigratedMultiInstancePostgresAppSyncStore(dsn=databases["target"], namespace="production", pool_size=2)
    try:
        assert restored.get_conversation(source["conversation_id"], actor="olivia")["title"] == "Quarterly plan"
        assert [e["seq"] for e in restored.sync_since(0)["events"]] == source["feed"]
        # Numbering continues above the restored events.
        assert restored.record_event("probe.event", "olivia", {"organization_id": "org_default"})["seq"] == source["feed"][-1] + 1
    finally:
        restored.close()


def test_restore_refuses_the_wrong_or_a_non_empty_database(databases: dict[str, str], tmp_path: Path) -> None:
    _populate(databases["source"])
    _populate(databases["other"])
    manifest_path, _ = pb.create_backup(tmp_path / "backups", targets=[pb.Target("appsync", "SRC", databases["source"])])
    with pytest.raises(pb.BackupError, match="confirm it explicitly"):
        pb.restore_backup(manifest_path, database="appsync", target_dsn=databases["target"], confirm_database=databases["source_name"])
    with pytest.raises(pb.BackupError, match="already has"):
        pb.restore_backup(manifest_path, database="appsync", target_dsn=databases["other"], confirm_database=databases["other_name"])
    report = pb.restore_backup(manifest_path, database="appsync", target_dsn=databases["other"], confirm_database=databases["other_name"], clean=True)
    assert report["ok"]


def test_tampered_archives_and_manifests_are_refused(databases: dict[str, str], tmp_path: Path) -> None:
    _populate(databases["source"])
    manifest_path, manifest = pb.create_backup(tmp_path / "backups", targets=[pb.Target("appsync", "SRC", databases["source"])], encryption_secret=SECRET, signing_secret=SIGNING)
    archive = manifest_path.parent / manifest["databases"][0]["archive"]
    original = archive.read_bytes()

    archive.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    with pytest.raises(pb.BackupError, match="checksum mismatch"):
        pb.verify_backup_set(manifest_path, encryption_secret=SECRET)
    archive.write_bytes(original)

    with pytest.raises(pb.BackupError, match="authentication"):
        pb.verify_backup_set(manifest_path, encryption_secret="a-different-secret-that-is-long-enough-000")

    forged = json.loads(manifest_path.read_text())
    forged["databases"][0]["row_counts"]["public.omnidesk_appsync_messages"] = 999
    manifest_path.write_text(json.dumps(forged))
    with pytest.raises(pb.BackupError, match="signature"):
        pb.verify_backup_set(manifest_path, encryption_secret=SECRET, signing_secret=SIGNING, require_signature=True)
    # Without the signature check, the restore itself catches the forged count.
    with pytest.raises(pb.BackupError, match="row counts differ"):
        pb.restore_backup(manifest_path, database="appsync", target_dsn=databases["target"], confirm_database=databases["target_name"], encryption_secret=SECRET)


def test_encryption_detects_truncation_and_reordering(tmp_path: Path) -> None:
    plain = tmp_path / "plain.bin"
    plain.write_bytes(os.urandom(pb._CHUNK * 2 + 123))
    sealed = tmp_path / "sealed.bin"
    pb.encrypt_file(plain, sealed, SECRET)
    pb.decrypt_file(sealed, tmp_path / "round.bin", SECRET)
    assert (tmp_path / "round.bin").read_bytes() == plain.read_bytes()

    data = sealed.read_bytes()
    header = len(pb._MAGIC) + pb._SALT + 4
    frame = pb._NONCE + 4 + pb._CHUNK + 16
    truncated = tmp_path / "truncated.bin"
    truncated.write_bytes(data[: header + 2 * frame])  # drops the final chunk
    with pytest.raises(pb.BackupError, match="truncated"):
        pb.decrypt_file(truncated, tmp_path / "x.bin", SECRET)
    swapped = tmp_path / "swapped.bin"
    swapped.write_bytes(data[:header] + data[header + frame : header + 2 * frame] + data[header : header + frame] + data[header + 2 * frame :])
    with pytest.raises(pb.BackupError, match="authentication"):
        pb.decrypt_file(swapped, tmp_path / "y.bin", SECRET)


def test_counts_and_dump_share_one_snapshot_under_concurrent_writes(databases: dict[str, str], tmp_path: Path) -> None:
    import psycopg

    _populate(databases["source"])
    with psycopg.connect(databases["source"], autocommit=True) as conn:
        conn.execute("CREATE TABLE busy(id BIGSERIAL PRIMARY KEY, note TEXT)")
    stop = threading.Event()

    def writer() -> None:
        with psycopg.connect(databases["source"], autocommit=True) as conn:
            while not stop.is_set():
                conn.execute("INSERT INTO busy(note) VALUES ('x')")

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        time.sleep(0.2)
        manifest_path, manifest = pb.create_backup(tmp_path / "backups", targets=[pb.Target("appsync", "SRC", databases["source"])])
    finally:
        stop.set()
        thread.join(timeout=10)
    recorded = manifest["databases"][0]["row_counts"]["public.busy"]
    assert recorded > 0
    report = pb.restore_backup(manifest_path, database="appsync", target_dsn=databases["target"], confirm_database=databases["target_name"])
    assert report["ok"]
    with psycopg.connect(databases["target"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM busy").fetchone()[0] == recorded


def test_passwords_never_reach_a_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    # The tool parses DSNs with psycopg; the release, staging and promotion gates
    # install only requirements.dev.lock, which has none.
    pytest.importorskip("psycopg")
    seen: list[tuple[list[str], dict[str, str]]] = []

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(args: list[str], **kwargs: Any) -> _Done:
        seen.append((list(args), dict(kwargs.get("env") or {})))
        return _Done()

    monkeypatch.setattr(pb.subprocess, "run", fake_run)
    monkeypatch.setattr(pb, "pg_tool", lambda name: name)
    conninfo, env = pb._connection_args("postgresql://omnidesk:s3cr3t-pass@db.internal:5432/omnidesk?sslmode=require")
    pb._run(["pg_dump", "--dbname", conninfo], env=env, what="probe")
    args, environment = seen[0]
    assert "s3cr3t-pass" not in " ".join(args)
    assert environment["PGPASSWORD"] == "s3cr3t-pass" and "sslmode=require" in conninfo


def test_retention_keeps_the_newest_sets_and_never_the_current(databases: dict[str, str], tmp_path: Path) -> None:
    _populate(databases["source"])
    dest = tmp_path / "backups"
    target = [pb.Target("appsync", "SRC", databases["source"])]
    first, _ = pb.create_backup(dest, targets=target)
    second, _ = pb.create_backup(dest, targets=target)
    third, manifest = pb.create_backup(dest, targets=target, keep=2)
    assert manifest["retention_purged"] == [first.parent.name]
    assert [path.name for path in pb.backup_sets(dest)] == [second.parent.name, third.parent.name]


def test_cli_backup_verify_restore_and_metrics(databases: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _populate(databases["source"])
    monkeypatch.setenv("OMNIDESK_APPSYNC_POSTGRES_DSN", databases["source"])
    monkeypatch.delenv("OMNIDESK_POSTGRES_DSN", raising=False)
    monkeypatch.setenv("OMNIDESK_BACKUP_ENCRYPTION_KEY", SECRET)
    monkeypatch.setenv("OMNIDESK_BACKUP_MANIFEST_KEY", SIGNING)
    metrics = tmp_path / "omnidesk_backup.prom"
    assert _script("backup_postgres").main(["--dest", str(tmp_path / "backups"), "--encrypt", "--sign-manifest", "--metrics-file", str(metrics)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] and summary["verified"] and [d["name"] for d in summary["databases"]] == ["appsync"]
    assert 'omnidesk_backup_verify_failure_total{kind="postgres"} 0' in metrics.read_text()

    verify = _script("verify_backup")
    assert verify.main([summary["manifest"], "--require-encryption", "--require-manifest-signature", "--metrics-file", str(metrics)]) == 0
    monkeypatch.setenv("OMNIDESK_RESTORE_POSTGRES_DSN", databases["target"])
    restore = _script("restore_postgres")
    report_path = tmp_path / "restore-report.json"
    assert restore.main(["--manifest", summary["manifest"], "--database", "appsync", "--confirm-database", databases["target_name"], "--require-signature", "--report", str(report_path)]) == 0
    assert json.loads(report_path.read_text())["ok"]

    archive = Path(summary["manifest"]).parent / summary["databases"][0]["archive"]
    archive.write_bytes(archive.read_bytes() + b"tampered")
    assert verify.main([summary["manifest"], "--metrics-file", str(metrics)]) == 1
    assert verify.main([summary["manifest"], "--metrics-file", str(metrics)]) == 1
    # The counter persists across runs, so the alert's increase() sees each failure.
    assert 'omnidesk_backup_verify_failure_total{kind="postgres"} 2' in metrics.read_text()


# ------------------------------------------------------------------ SQLite ---


def test_sqlite_discovery_backs_up_every_runtime_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    state.mkdir()
    for name in ("runs", "outbound_messages", "jobs", "token_budget"):
        with closing(sqlite3.connect(state / f"{name}.sqlite3")) as con:
            con.execute("CREATE TABLE t(v TEXT)")
            con.commit()
    monkeypatch.setenv("OMNIDESK_BACKUP_ENCRYPTION_KEY", SECRET)
    assert _script("backup_sqlite").main(["--dest", str(tmp_path / "backup"), "--encrypt", "--discover", str(state)]) == 0
    manifest = json.loads((tmp_path / "backup" / "backup_manifest.json").read_text())
    assert sorted(Path(item["source"]).name for item in manifest["items"]) == ["jobs.sqlite3", "outbound_messages.sqlite3", "runs.sqlite3", "token_budget.sqlite3"]
    assert all(item["ok"] for item in manifest["items"])


def test_sqlite_restore_moves_a_stale_wal_aside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backup_source = tmp_path / "good.sqlite3"
    with closing(sqlite3.connect(backup_source)) as con:
        con.execute("CREATE TABLE t(v TEXT)")
        con.execute("INSERT INTO t VALUES ('from backup')")
        con.commit()
    target = tmp_path / "live.sqlite3"
    shutil.copy2(backup_source, target)
    (tmp_path / "live.sqlite3-wal").write_bytes(b"stale wal from the replaced database")
    (tmp_path / "live.sqlite3-shm").write_bytes(b"stale shm")
    assert _script("restore_sqlite").main([str(backup_source), str(target), "--force"]) == 0
    assert not (tmp_path / "live.sqlite3-wal").exists() and not (tmp_path / "live.sqlite3-shm").exists()
    moved = sorted(path.name for path in tmp_path.glob("live.sqlite3.pre-restore-*"))
    assert len(moved) == 3 and any(name.endswith("-wal") for name in moved)
    with closing(sqlite3.connect(target)) as con:
        assert con.execute("SELECT v FROM t").fetchall() == [("from backup",)]


# ---------------------------------------------------- client tool versions ---


def _fake_tool(directory: Path, name: str, major: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / name
    tool.write_text(f"#!/bin/sh\necho '{name} (PostgreSQL) {major}.3'\n", encoding="utf-8")
    tool.chmod(0o755)
    return tool


def test_client_tools_must_match_the_servers_major(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # CI had pg_dump/pg_restore 17 against a PostgreSQL 16 server: every restore
    # failed on "unrecognized configuration parameter transaction_timeout".
    newer = _fake_tool(tmp_path / "pg17" / "bin", "pg_restore", 17)
    matching = _fake_tool(tmp_path / "pg16" / "bin", "pg_restore", 16)
    monkeypatch.setenv("OMNIDESK_PG_BIN", str(newer.parent))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(pb, "_PG_BIN_LAYOUTS", (str(tmp_path / "pg{major}" / "bin"),))
    # The server's version is found in its usual install location.
    assert pb.pg_tool("pg_restore", server_major=16) == str(matching)
    assert pb.pg_tool("pg_restore", server_major=17) == str(newer)
    # Without a known server version, OMNIDESK_PG_BIN wins as before.
    assert pb.pg_tool("pg_restore") == str(newer)
    matching.unlink()
    with pytest.raises(pb.BackupError, match=r"must be PostgreSQL 16's.*pg17/bin/pg_restore \(17\)"):
        pb.pg_tool("pg_restore", server_major=16)
