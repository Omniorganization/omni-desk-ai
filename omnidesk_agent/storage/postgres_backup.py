"""Logical PostgreSQL backups that can be verified and restored with confidence.

A backup set is a directory with one ``pg_dump`` custom-format archive per
database and a ``manifest.json``. For each database the manifest records the
archive's SHA-256 (and, when encrypted, the plaintext's), its table-of-contents
size, the AppSync schema versions, and an exact row count of every table.

Row counts and the dump read the **same snapshot**: the backup opens a
repeatable-read transaction, exports its snapshot, counts rows in it, and runs
``pg_dump --snapshot`` against it. Concurrent writes therefore cannot make a
correct restore look wrong, or a wrong one look right.

Archives can be encrypted with AES-256-GCM in independently authenticated
chunks (key derived with HKDF-SHA256 from a secret of at least 32 characters
and a per-file salt); chunk order and truncation are authenticated too. The
manifest can be HMAC-signed.

Restoring verifies the archive first, refuses a target whose database name was
not confirmed or that already has tables (unless ``clean``), runs
``pg_restore --single-transaction --exit-on-error``, and then checks every row
count and schema version against the manifest.

Passwords are passed to the PostgreSQL tools through ``PGPASSWORD``, never on
their command line.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

MANIFEST_FORMAT = "omnidesk-postgres-backup/v1"
MANIFEST_NAME = "manifest.json"
DEFAULT_TARGETS: tuple[tuple[str, str], ...] = (
    ("core", "OMNIDESK_POSTGRES_DSN"),
    ("appsync", "OMNIDESK_APPSYNC_POSTGRES_DSN"),
)
APPSYNC_MIGRATION_TABLE = "omnidesk_appsync_schema_migrations"

_MAGIC = b"OMNIPGE1"
_CHUNK = 1024 * 1024
_SALT = 16
_NONCE = 12


class BackupError(RuntimeError):
    """A backup, verification or restore step failed; the message says which."""


# ------------------------------------------------------------------ tools ---


# Client tools must be of the server's major version. A newer pg_dump writes
# archives an older server cannot restore (pg_dump 17 sets transaction_timeout,
# which PostgreSQL 16 rejects), and an older pg_restore cannot read a newer
# archive format. So the usual install locations of the server's version are
# searched as well as OMNIDESK_PG_BIN and PATH.
_PG_BIN_LAYOUTS = (
    "/usr/lib/postgresql/{major}/bin",  # Debian, Ubuntu
    "/usr/pgsql-{major}/bin",  # RHEL, Fedora (PGDG packages)
    "/opt/homebrew/opt/postgresql@{major}/bin",  # macOS Homebrew
    "/usr/local/opt/postgresql@{major}/bin",
)
_TOOL_VERSION_RE = re.compile(r"\(PostgreSQL\)\s+([0-9]+)")


def _tool_major(path: str) -> Optional[int]:
    result = subprocess.run([path, "--version"], capture_output=True, text=True, check=False)  # nosec B603 - fixed tool, no shell
    match = _TOOL_VERSION_RE.search(result.stdout)
    return int(match.group(1)) if match else None


def pg_tool(name: str, *, server_major: Optional[int] = None) -> str:
    """Locate a PostgreSQL client tool: ``OMNIDESK_PG_BIN`` first, then PATH.

    With ``server_major`` the tool must be of that major version; the usual
    install locations of that version are searched too, and a mismatch fails.
    """
    directory = os.getenv("OMNIDESK_PG_BIN", "").strip()
    candidates = [str(Path(directory) / name)] if directory else []
    if server_major is not None:
        candidates += [str(Path(layout.format(major=server_major)) / name) for layout in _PG_BIN_LAYOUTS]
    found = shutil.which(name)
    if found:
        candidates.append(found)
    existing = [candidate for candidate in dict.fromkeys(candidates) if Path(candidate).is_file()]
    if not existing:
        raise BackupError(f"{name} not found; install the PostgreSQL client tools or set OMNIDESK_PG_BIN")
    if server_major is None:
        return existing[0]
    majors = {candidate: _tool_major(candidate) for candidate in existing}
    for candidate, major in majors.items():
        if major == server_major:
            return candidate
    seen = ", ".join(f"{path} ({major or 'unknown version'})" for path, major in majors.items())
    raise BackupError(
        f"{name} must be PostgreSQL {server_major}'s, like the server; found {seen}. "
        f"Install the PostgreSQL {server_major} client tools or set OMNIDESK_PG_BIN to their bin directory"
    )


def tool_version(name: str, *, server_major: Optional[int] = None) -> str:
    result = subprocess.run([pg_tool(name, server_major=server_major), "--version"], capture_output=True, text=True, check=False)  # nosec B603 - fixed tool, no shell
    return result.stdout.strip()


def server_major_of(dsn: str) -> int:
    import psycopg  # type: ignore

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_setting('server_version_num')::int / 10000")
        return int(cur.fetchone()[0])


def _connection_args(dsn: str) -> tuple[str, dict[str, str]]:
    """A conninfo string without the password, and the environment that carries it."""
    from psycopg import conninfo  # type: ignore

    params = conninfo.conninfo_to_dict(dsn)
    password = params.pop("password", None)
    env = dict(os.environ)
    if password:
        env["PGPASSWORD"] = str(password)
    return conninfo.make_conninfo(**params), env


def _run(args: list[str], *, env: dict[str, str], what: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, env=env, check=False)  # nosec B603 - fixed tool, argument list, no shell
    if result.returncode != 0:
        raise BackupError(f"{what} failed (exit {result.returncode}): {result.stderr.strip()[-2000:]}")
    return result


# ------------------------------------------------------------- encryption ---


def _derive_key(secret: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    if len(secret) < 32:
        raise BackupError("backup encryption secret must be at least 32 characters")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"omnidesk-postgres-backup-v1").derive(secret.encode("utf-8"))


def encrypt_file(source: Path, target: Path, secret: str) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = secrets.token_bytes(_SALT)
    header = _MAGIC + salt + struct.pack(">I", _CHUNK)
    aead = AESGCM(_derive_key(secret, salt))
    with source.open("rb") as reader, target.open("wb") as writer:
        writer.write(header)
        index = 0
        chunk = reader.read(_CHUNK)
        while True:
            following = reader.read(_CHUNK) if chunk else b""
            final = not following
            nonce = secrets.token_bytes(_NONCE)
            sealed = aead.encrypt(nonce, chunk, header + struct.pack(">QB", index, int(final)))
            writer.write(nonce + struct.pack(">I", len(sealed)) + sealed)
            if final:
                return
            index += 1
            chunk = following


def decrypt_file(source: Path, target: Path, secret: str) -> None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    with source.open("rb") as reader, target.open("wb") as writer:
        header = reader.read(len(_MAGIC) + _SALT + 4)
        if len(header) != len(_MAGIC) + _SALT + 4 or not header.startswith(_MAGIC):
            raise BackupError("not an OmniDesk encrypted backup")
        aead = AESGCM(_derive_key(secret, header[len(_MAGIC):len(_MAGIC) + _SALT]))
        index = 0
        while True:
            framing = reader.read(_NONCE + 4)
            if len(framing) < _NONCE + 4:
                raise BackupError("encrypted backup is truncated")
            (length,) = struct.unpack(">I", framing[_NONCE:])
            sealed = reader.read(length)
            if len(sealed) != length:
                raise BackupError("encrypted backup is truncated")
            plaintext = None
            for final in (0, 1):
                try:
                    plaintext = aead.decrypt(framing[:_NONCE], sealed, header + struct.pack(">QB", index, final))
                except InvalidTag:
                    continue
                writer.write(plaintext)
                if final:
                    if reader.read(1):
                        raise BackupError("data after the final chunk of the encrypted backup")
                    return
                break
            if plaintext is None:
                raise BackupError("encrypted backup failed authentication (wrong key, or modified)")
            index += 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        for block in iter(lambda: reader.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------- manifest ---


def _signature(manifest: dict[str, Any], secret: str) -> str:
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hmac_sha256"}
    payload = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def sign_manifest(manifest: dict[str, Any], secret: str) -> None:
    if len(secret) < 32:
        raise BackupError("manifest signing secret must be at least 32 characters")
    manifest["manifest_hmac_sha256"] = _signature(manifest, secret)


def manifest_signature_valid(manifest: dict[str, Any], secret: str) -> bool:
    expected = str(manifest.get("manifest_hmac_sha256") or "")
    return bool(expected) and len(secret) >= 32 and hmac.compare_digest(_signature(manifest, secret), expected)


# ------------------------------------------------------------------ counts ---


def _tables(cur: Any) -> list[tuple[str, str]]:
    cur.execute(
        """
        SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%%'
        ORDER BY 1, 2
        """
    )
    return [(str(row[0]), str(row[1])) for row in cur.fetchall()]


def row_counts(cur: Any) -> dict[str, int]:
    from psycopg import sql  # type: ignore

    counts: dict[str, int] = {}
    for schema, table in _tables(cur):
        cur.execute(sql.SQL("SELECT COUNT(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table)))
        counts[f"{schema}.{table}"] = int(cur.fetchone()[0])
    return counts


def schema_versions(cur: Any) -> dict[str, int]:
    cur.execute("SELECT to_regclass(%s)", (APPSYNC_MIGRATION_TABLE,))
    if cur.fetchone()[0] is None:
        return {}
    cur.execute(f"SELECT namespace, MAX(version) FROM {APPSYNC_MIGRATION_TABLE} GROUP BY namespace ORDER BY namespace")  # nosec B608 - fixed table name
    return {str(row[0]): int(row[1]) for row in cur.fetchall()}


def toc_entries(archive: Path, *, server_major: Optional[int] = None) -> int:
    listing = _run([pg_tool("pg_restore", server_major=server_major), "--list", str(archive)], env=dict(os.environ), what="pg_restore --list")
    return sum(1 for line in listing.stdout.splitlines() if line.strip() and not line.startswith(";"))


# ------------------------------------------------------------------ backup ---


@dataclass(frozen=True)
class Target:
    name: str
    dsn_env: str
    dsn: str


def resolve_targets(specs: Optional[list[str]] = None) -> list[Target]:
    """``name=ENV`` specs, or every default target whose DSN is set. The same DSN
    under two names is backed up once."""
    pairs = [tuple(spec.split("=", 1)) for spec in specs] if specs else list(DEFAULT_TARGETS)
    targets: list[Target] = []
    seen: set[str] = set()
    for pair in pairs:
        if len(pair) != 2 or not pair[0] or not pair[1]:
            raise BackupError("database targets are name=DSN_ENV")
        name, env = pair[0], pair[1]
        dsn = os.getenv(env, "").strip()
        if not dsn:
            if specs:
                raise BackupError(f"missing PostgreSQL DSN environment variable: {env}")
            continue
        if dsn in seen:
            continue
        seen.add(dsn)
        targets.append(Target(name=name, dsn_env=env, dsn=dsn))
    if not targets:
        raise BackupError("no PostgreSQL DSN is set (OMNIDESK_POSTGRES_DSN, OMNIDESK_APPSYNC_POSTGRES_DSN)")
    return targets


def _backup_database(target: Target, directory: Path, *, secret: Optional[str]) -> dict[str, Any]:
    import psycopg  # type: ignore

    started = time.time()
    plain = directory / f"{target.name}.dump"
    conninfo, env = _connection_args(target.dsn)
    with psycopg.connect(target.dsn) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_export_snapshot(), current_database(), current_setting('server_version'), current_setting('server_version_num')::int / 10000")
            snapshot, database, server_version, server_major = cur.fetchone()
            counts = row_counts(cur)
            versions = schema_versions(cur)
            # The dump reads the exported snapshot while this transaction holds it open.
            _run(
                [pg_tool("pg_dump", server_major=int(server_major)), "--format=custom", "--no-owner", "--no-acl", f"--snapshot={snapshot}", "--file", str(plain), "--dbname", conninfo],
                env=env, what=f"pg_dump of {target.name}",
            )
        conn.rollback()
    entry: dict[str, Any] = {
        "name": target.name,
        "dsn_env": target.dsn_env,
        "database": str(database),
        "server_version": str(server_version),
        "server_major": int(server_major),
        "pg_dump_version": tool_version("pg_dump", server_major=int(server_major)),
        "plaintext_sha256": sha256_file(plain),
        "toc_entries": toc_entries(plain, server_major=int(server_major)),
        "row_counts": counts,
        "schema_versions": versions,
        "encrypted": bool(secret),
    }
    if secret:
        sealed = directory / f"{target.name}.dump.enc"
        encrypt_file(plain, sealed, secret)
        plain.unlink()
        entry["archive"] = sealed.name
    else:
        entry["archive"] = plain.name
    archive = directory / str(entry["archive"])
    entry["sha256"] = sha256_file(archive)
    entry["bytes"] = archive.stat().st_size
    entry["duration_seconds"] = round(time.time() - started, 3)
    return entry


def create_backup(
    dest: Path,
    *,
    targets: list[Target],
    encryption_secret: Optional[str] = None,
    signing_secret: Optional[str] = None,
    keep: int = 0,
    retention_days: float = 0,
) -> tuple[Path, dict[str, Any]]:
    """Write one backup set under ``dest``; returns its manifest path and manifest."""
    dest = Path(dest).expanduser()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    directory = dest / f"{stamp}-{secrets.token_hex(3)}"
    directory.mkdir(parents=True, exist_ok=False)
    os.chmod(directory, 0o700)
    manifest: dict[str, Any] = {
        "format": MANIFEST_FORMAT,
        "created_at": time.time(),
        "databases": [_backup_database(target, directory, secret=encryption_secret) for target in targets],
    }
    if signing_secret:
        sign_manifest(manifest, signing_secret)
    path = directory / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    manifest["retention_purged"] = apply_retention(dest, keep=keep, retention_days=retention_days, current=directory)
    return path, manifest


def _created_at(directory: Path) -> float:
    try:
        return float(json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8")).get("created_at") or 0.0)
    except (OSError, ValueError):
        return (directory / MANIFEST_NAME).stat().st_mtime


def backup_sets(dest: Path) -> list[Path]:
    """Backup set directories under ``dest`` (those with a manifest), oldest first."""
    if not dest.exists():
        return []
    sets = [path for path in dest.iterdir() if path.is_dir() and (path / MANIFEST_NAME).is_file()]
    return sorted(sets, key=lambda path: (_created_at(path), path.name))


def apply_retention(dest: Path, *, keep: int, retention_days: float, current: Optional[Path] = None) -> list[str]:
    """Delete backup sets beyond the newest ``keep`` and older than ``retention_days``.
    The set just written is never deleted."""
    sets = backup_sets(dest)
    doomed: list[Path] = []
    if keep > 0:
        doomed.extend(sets[:-keep])
    if retention_days > 0:
        cutoff = time.time() - retention_days * 86_400
        doomed.extend(path for path in sets if _created_at(path) < cutoff)
    purged: list[str] = []
    for path in sorted(set(doomed)):
        if current is not None and path.resolve() == current.resolve():
            continue
        shutil.rmtree(path)
        purged.append(path.name)
    return purged


# ----------------------------------------------------------------- verify ---


def load_manifest(path: Path, *, signing_secret: Optional[str] = None, require_signature: bool = False) -> dict[str, Any]:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if manifest.get("format") != MANIFEST_FORMAT:
        raise BackupError(f"not a {MANIFEST_FORMAT} manifest")
    if require_signature and not manifest_signature_valid(manifest, signing_secret or ""):
        raise BackupError("manifest signature missing or invalid")
    return manifest


def _entry(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    for entry in manifest.get("databases", []):
        if entry.get("name") == name:
            return dict(entry)
    raise BackupError(f"backup set has no database named {name!r}")


@dataclass
class _Readable:
    """A verified plaintext archive; deleted afterwards if it was decrypted."""

    path: Path
    temporary: bool

    def close(self) -> None:
        if self.temporary:
            self.path.unlink(missing_ok=True)


def open_archive(manifest_path: Path, entry: dict[str, Any], *, encryption_secret: Optional[str]) -> _Readable:
    archive = Path(manifest_path).parent / str(entry["archive"])
    if not archive.is_file():
        raise BackupError(f"archive missing: {archive.name}")
    if sha256_file(archive) != entry.get("sha256"):
        raise BackupError(f"archive checksum mismatch: {archive.name}")
    if not entry.get("encrypted"):
        if sha256_file(archive) != entry.get("plaintext_sha256"):
            raise BackupError(f"archive checksum mismatch: {archive.name}")
        return _Readable(archive, False)
    if not encryption_secret:
        raise BackupError("the archive is encrypted; the encryption secret is required")
    handle, name = tempfile.mkstemp(prefix=f"omnidesk-restore-{entry['name']}-", suffix=".dump")
    os.close(handle)
    plain = Path(name)
    try:
        decrypt_file(archive, plain, encryption_secret)
        if sha256_file(plain) != entry.get("plaintext_sha256"):
            raise BackupError(f"decrypted archive checksum mismatch: {archive.name}")
    except BaseException:
        plain.unlink(missing_ok=True)
        raise
    return _Readable(plain, True)


def verify_backup_set(manifest_path: Path, *, encryption_secret: Optional[str] = None, signing_secret: Optional[str] = None, require_signature: bool = False) -> dict[str, Any]:
    """Checksums, decryption, and a readable table of contents for every archive."""
    manifest = load_manifest(manifest_path, signing_secret=signing_secret, require_signature=require_signature)
    results: list[dict[str, Any]] = []
    for entry in manifest.get("databases", []):
        readable = open_archive(manifest_path, entry, encryption_secret=encryption_secret)
        try:
            # The archive is read with the tools of the version that wrote it.
            recorded = entry.get("server_major")
            entries = toc_entries(readable.path, server_major=int(recorded) if recorded else None)
        finally:
            readable.close()
        if entries != entry.get("toc_entries"):
            raise BackupError(f"{entry['name']}: table of contents has {entries} entries, manifest says {entry.get('toc_entries')}")
        results.append({"name": entry["name"], "toc_entries": entries, "encrypted": bool(entry.get("encrypted"))})
    return {"ok": True, "databases": results, "created_at": manifest.get("created_at")}


# ----------------------------------------------------------------- restore ---


def _target_state(dsn: str) -> tuple[str, int]:
    import psycopg  # type: ignore

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        database = str(cur.fetchone()[0])
        return database, len(_tables(cur))


def _restored_state(dsn: str) -> tuple[dict[str, int], dict[str, int]]:
    import psycopg  # type: ignore

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        return row_counts(cur), schema_versions(cur)


def restore_backup(
    manifest_path: Path,
    *,
    database: str,
    target_dsn: str,
    confirm_database: str,
    clean: bool = False,
    encryption_secret: Optional[str] = None,
    signing_secret: Optional[str] = None,
    require_signature: bool = False,
) -> dict[str, Any]:
    """Restore one database of a backup set into ``target_dsn`` and prove it."""
    started = time.time()
    manifest = load_manifest(manifest_path, signing_secret=signing_secret, require_signature=require_signature)
    entry = _entry(manifest, database)
    actual_name, existing_tables = _target_state(target_dsn)
    if actual_name != confirm_database:
        raise BackupError(f"target database is {actual_name!r}; confirm it explicitly to restore into it")
    if existing_tables and not clean:
        raise BackupError(f"target database {actual_name!r} already has {existing_tables} tables; restore into an empty database or pass clean")
    readable = open_archive(manifest_path, entry, encryption_secret=encryption_secret)
    try:
        conninfo, env = _connection_args(target_dsn)
        args = [pg_tool("pg_restore", server_major=server_major_of(target_dsn)), "--no-owner", "--no-acl", "--exit-on-error", "--single-transaction"]
        if clean:
            args += ["--clean", "--if-exists"]
        _run([*args, "--dbname", conninfo, str(readable.path)], env=env, what=f"pg_restore of {database}")
    finally:
        readable.close()
    counts, versions = _restored_state(target_dsn)
    expected_counts: dict[str, int] = {str(k): int(v) for k, v in dict(entry.get("row_counts") or {}).items()}
    mismatched = {table: {"expected": count, "restored": counts.get(table)} for table, count in expected_counts.items() if counts.get(table) != count}
    if mismatched:
        raise BackupError(f"restored row counts differ from the backup: {json.dumps(mismatched, sort_keys=True)}")
    expected_versions = {str(k): int(v) for k, v in dict(entry.get("schema_versions") or {}).items()}
    if versions != expected_versions:
        raise BackupError(f"restored schema versions {versions} differ from the backup {expected_versions}")
    finished = time.time()
    return {
        "ok": True,
        "database": database,
        "target_database": actual_name,
        "tables_verified": len(expected_counts),
        "rows_verified": sum(expected_counts.values()),
        "schema_versions": versions,
        "backup_created_at": manifest.get("created_at"),
        "restore_seconds": round(finished - started, 3),
        "backup_age_seconds": round(finished - float(manifest.get("created_at") or finished), 3),
    }


# ----------------------------------------------------------------- metrics ---


def record_verification(metrics_file: Optional[Path], *, kind: str, ok: bool) -> None:
    """Update a node-exporter textfile: the failure counter the
    ``OmniDeskBackupVerifyFailure`` alert watches, and the last success time."""
    if metrics_file is None:
        return
    path = Path(metrics_file)
    failures: dict[str, float] = {}
    successes: dict[str, float] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            for name, table in (("omnidesk_backup_verify_failure_total", failures), ("omnidesk_backup_last_verify_success_timestamp_seconds", successes)):
                prefix = name + '{kind="'
                if line.startswith(prefix):
                    label, _, value = line[len(prefix):].partition('"} ')
                    try:
                        table[label] = float(value)
                    except ValueError:
                        continue
    failures.setdefault(kind, 0.0)
    if ok:
        successes[kind] = time.time()
    else:
        failures[kind] += 1
    lines = ["# TYPE omnidesk_backup_verify_failure_total counter"]
    lines += [f'omnidesk_backup_verify_failure_total{{kind="{label}"}} {value:g}' for label, value in sorted(failures.items())]
    lines += ["# TYPE omnidesk_backup_last_verify_success_timestamp_seconds gauge"]
    lines += [f'omnidesk_backup_last_verify_success_timestamp_seconds{{kind="{label}"}} {value:.3f}' for label, value in sorted(successes.items())]
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(path)


def iter_databases(manifest: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield from (dict(entry) for entry in manifest.get("databases", []))
