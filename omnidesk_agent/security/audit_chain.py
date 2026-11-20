"""Keyed, tamper-evident audit log: an HMAC-SHA256 hash chain.

Each line of an audit log is one JSON entry. An entry's ``hash`` is an
HMAC-SHA256, under a key derived from the audit key in the secret store, of the
canonical JSON of the entry without ``hash``. Every entry carries the previous
entry's ``hash`` and a sequence number, so changing, inserting, deleting or
reordering entries breaks verification, and nobody without the key can rebuild
the chain. Cutting entries off the end leaves a valid shorter chain; signed
checkpoints (``WormAuditCheckpoint.create_anchor``) record a sequence number and
hash, so a later verification against them catches that too.

Writers fail closed. When the last line is torn or not JSON, when an unchained
line follows the chain, or when the last entry fails its HMAC (tampered, or
written with another key), ``append`` raises ``AuditIntegrityError`` instead of
starting over, and callers must refuse the action they wanted to audit.
``rotate`` seals such a log (or a full one) under a new name and starts a new
chain whose first entry commits to the sealed file's SHA-256.

A log written before this format existed (unkeyed SHA-256 chain, unchained
break-glass lines) is upgraded on the first append: the first keyed entry
commits to the exact bytes of everything before it.

The key comes from an environment variable that the deployment fills from its
secret store (``OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY`` by default). Outside
production, when it is unset, a random per-log key is generated once and kept
next to the log (``<log>.key``, mode 0600); that only protects against accidents
and casual edits, because anyone who can read the key file can rebuild the chain.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

# One class for callers on every branch of the API (security/audit_errors.py).
from omnidesk_agent.security.audit_errors import AuditIntegrityError

if sys.platform == "win32":  # pragma: no cover - Windows
    import msvcrt
else:
    import fcntl

CHAIN_FORMAT = "omnidesk-audit-hmac-sha256-v1"
GENESIS_EVENT = "audit.chain_genesis"
ZERO_HASH = "0" * 64
DEFAULT_KEY_ENV = "OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY"
_CHAIN_KEY_LABEL = b"omnidesk/audit-chain/v1"
_KEY_ID_LABEL = b"omnidesk/audit-chain/key-id"
_TAIL_CHUNK_BYTES = 8192
_PRODUCTION_ENV_VALUES = frozenset({"prod", "production"})
_ENTRY_FIELDS = frozenset({"chain", "seq", "ts", "event", "payload", "previous_hash", "key_id", "hash"})


# --- keys ------------------------------------------------------------------------------------


def is_production_environment(environ: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    if str(env.get("OMNIDESK_REQUIRE_PRODUCTION_GUARDS", "")).strip().lower() in {"1", "true", "yes", "on"}:
        return True
    mode = (env.get("OMNIDESK_ENV") or env.get("APP_ENV") or env.get("ENV") or "").strip().lower()
    return mode in _PRODUCTION_ENV_VALUES


def dev_key_file_for(log_path: Path) -> Path:
    log_path = Path(log_path).expanduser()
    return log_path.with_name(log_path.name + ".key")


def load_audit_key(
    key_env: str = DEFAULT_KEY_ENV,
    *,
    dev_key_file: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> bytes:
    """Return the raw audit key: the secret-store value, or the local dev key outside production."""
    env = os.environ if environ is None else environ
    value = str(env.get(key_env, "") or "").strip()
    if value:
        return value.encode("utf-8")
    if is_production_environment(env) or dev_key_file is None:
        raise AuditIntegrityError(f"audit HMAC key is not configured: {key_env}")
    return _load_or_create_dev_key(Path(dev_key_file))


def _load_or_create_dev_key(path: Path) -> bytes:
    path = path.expanduser()
    for _attempt in range(100):
        try:
            data = path.read_bytes().strip()
        except FileNotFoundError:
            data = b""
        else:
            if data:
                return data
            # Another process created the file and has not written it yet.
            time.sleep(0.01)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        key = secrets.token_hex(32).encode("ascii")
        with os.fdopen(fd, "wb") as handle:
            handle.write(key + b"\n")
        return key
    raise AuditIntegrityError(f"audit dev key file is empty: {path}")


def derive_chain_key(raw_key: bytes) -> bytes:
    """Chain sub-key; checkpoints sign with the raw key, so the two never share a key."""
    return hmac.new(raw_key, _CHAIN_KEY_LABEL, hashlib.sha256).digest()


def key_id_for(chain_key: bytes) -> str:
    return hmac.new(chain_key, _KEY_ID_LABEL, hashlib.sha256).hexdigest()[:16]


# --- entries ---------------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return str(value)


def json_safe(value: Any) -> Any:
    """Plain JSON types only (string keys), so the stored line re-encodes to the same bytes."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))


def canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def entry_line(entry: Mapping[str, Any]) -> bytes:
    # Readable layout (as before the chain existed); the HMAC covers canonical_json.
    return json.dumps(entry, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"


def entry_mac(chain_key: bytes, entry: Mapping[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hmac.new(chain_key, canonical_json(body), hashlib.sha256).hexdigest()


def _check_entry(entry: Any, chain_key: bytes, key_id: str) -> Optional[str]:
    """Reason the entry is not a valid chained entry under this key, or None."""
    if not isinstance(entry, dict) or entry.get("chain") != CHAIN_FORMAT:
        return "line is not a keyed audit chain entry"
    if set(entry) != _ENTRY_FIELDS:
        return "entry fields do not match the chain format"
    if not isinstance(entry.get("seq"), int) or isinstance(entry.get("seq"), bool) or entry["seq"] < 0:
        return "entry sequence number is invalid"
    if entry.get("key_id") != key_id:
        return "entry was written with a different audit key"
    mac = entry.get("hash")
    if not isinstance(mac, str) or not hmac.compare_digest(mac, entry_mac(chain_key, entry)):
        return "entry HMAC does not verify"
    return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- file locking ----------------------------------------------------------------------------


@contextlib.contextmanager
def _exclusive_lock(log_path: Path) -> Iterator[None]:
    """Serialize writers across threads and processes (sidecar ``<log>.lock``)."""
    lock_path = log_path.with_name(log_path.name + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if sys.platform == "win32":  # pragma: no cover - Windows
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if sys.platform == "win32":  # pragma: no cover - Windows
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _read_all(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _read_last_line(fd: int, size: int) -> tuple[bytes, bool]:
    """Return the last line (without its newline) and whether the file ends with a newline."""
    pos = size
    buffer = b""
    while pos > 0:
        step = min(_TAIL_CHUNK_BYTES, pos)
        pos -= step
        os.lseek(fd, pos, os.SEEK_SET)
        buffer = os.read(fd, step) + buffer
        body = buffer[:-1] if buffer.endswith(b"\n") else buffer
        cut = body.rfind(b"\n")
        if cut >= 0:
            return body[cut + 1 :], buffer.endswith(b"\n")
    return (buffer[:-1] if buffer.endswith(b"\n") else buffer), buffer.endswith(b"\n")


# --- writer ----------------------------------------------------------------------------------


class AuditChain:
    """Append keyed, hash-chained entries to one audit log file."""

    def __init__(
        self,
        path: Path,
        *,
        key_env: str = DEFAULT_KEY_ENV,
        key: Optional[bytes] = None,
        environ: Optional[Mapping[str, str]] = None,
    ):
        self.path = Path(path).expanduser()
        self.key_env = key_env
        self._raw_key = key
        self._environ = environ
        self._chain_key: Optional[bytes] = None
        self._lock = threading.Lock()
        # (inode, size, mtime_ns) of the file after our last write -> (seq, hash).
        self._tail: Optional[tuple[tuple[int, int, int], int, str]] = None

    def chain_key(self) -> bytes:
        if self._chain_key is None:
            raw = self._raw_key if self._raw_key is not None else load_audit_key(
                self.key_env, dev_key_file=dev_key_file_for(self.path), environ=self._environ
            )
            self._chain_key = derive_chain_key(raw)
        return self._chain_key

    def append(self, event: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        chain_key = self.chain_key()
        clean = json_safe(dict(payload))
        with self._lock, self._open_locked() as fd:
            seq, previous = self._tail_state(fd, chain_key)
            entry = self._entry(chain_key, seq + 1, previous, event, clean)
            _write_all(fd, entry_line(entry))
            self._remember(fd, entry)
        return entry

    def check(self) -> tuple[int, str]:
        """Raise AuditIntegrityError unless the log can be extended; return (seq, hash) of its tail."""
        chain_key = self.chain_key()
        with self._lock, self._open_locked() as fd:
            return self._tail_state(fd, chain_key)

    @contextlib.contextmanager
    def _open_locked(self) -> Iterator[int]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self.path):
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                yield fd
            finally:
                os.close(fd)

    def _tail_state(self, fd: int, chain_key: bytes) -> tuple[int, str]:
        stat = os.fstat(fd)
        signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if self._tail is not None and self._tail[0] == signature:
            return self._tail[1], self._tail[2]
        if stat.st_size == 0:
            return self._write_genesis(fd, chain_key, {"reason": "new_log", **_prefix_summary(b"")})
        line, newline = _read_last_line(fd, stat.st_size)
        if not newline:
            raise AuditIntegrityError(f"audit log ends with a partial line: {self.path}")
        try:
            last = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise AuditIntegrityError(f"last audit log line is not valid JSON: {self.path}") from exc
        if not isinstance(last, dict) or last.get("chain") != CHAIN_FORMAT:
            data = _read_all(fd)
            if _first_chained_offset(data) is not None:
                raise AuditIntegrityError(f"an unchained line follows the keyed audit chain: {self.path}")
            # Pre-chain log: the first keyed entry commits to its exact bytes.
            return self._write_genesis(fd, chain_key, {"reason": "legacy_upgrade", **_prefix_summary(data)})
        reason = _check_entry(last, chain_key, key_id_for(chain_key))
        if reason is not None:
            raise AuditIntegrityError(f"last audit log entry is not trustworthy ({reason}): {self.path}")
        return int(last["seq"]), str(last["hash"])

    def _write_genesis(self, fd: int, chain_key: bytes, payload: dict[str, Any]) -> tuple[int, str]:
        entry = self._entry(chain_key, 0, ZERO_HASH, GENESIS_EVENT, json_safe(payload))
        _write_all(fd, entry_line(entry))
        self._remember(fd, entry)
        return 0, str(entry["hash"])

    @staticmethod
    def _entry(chain_key: bytes, seq: int, previous: str, event: str, payload: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "chain": CHAIN_FORMAT,
            "seq": seq,
            "ts": time.time(),
            "event": str(event),
            "payload": payload,
            "previous_hash": previous,
            "key_id": key_id_for(chain_key),
        }
        entry["hash"] = entry_mac(chain_key, entry)
        return entry

    def _remember(self, fd: int, entry: Mapping[str, Any]) -> None:
        stat = os.fstat(fd)
        self._tail = ((stat.st_ino, stat.st_size, stat.st_mtime_ns), int(entry["seq"]), str(entry["hash"]))


def _prefix_summary(data: bytes) -> dict[str, Any]:
    lines = data.splitlines()
    unparseable = 0
    for raw in lines:
        try:
            json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            unparseable += 1
    return {"prefix_bytes": len(data), "prefix_sha256": _sha256(data), "prefix_lines": len(lines), "prefix_unparseable_lines": unparseable}


def _first_chained_offset(data: bytes) -> Optional[int]:
    offset = 0
    for raw in data.splitlines(keepends=True):
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict) and parsed.get("chain") == CHAIN_FORMAT:
            return offset
        offset += len(raw)
    return None


def read_audit_events(path: Path) -> list[dict[str, Any]]:
    """Entries as flat records (payload fields plus event, ts, seq), without genesis entries.

    Reading only: this does not verify anything; use verify_audit_log for that.
    Pre-chain lines are returned as they are.
    """
    events: list[dict[str, Any]] = []
    for raw in Path(path).expanduser().read_bytes().splitlines():
        record = json.loads(raw.decode("utf-8"))
        if isinstance(record, dict) and record.get("chain") == CHAIN_FORMAT:
            if record.get("event") == GENESIS_EVENT:
                continue
            payload = record.get("payload")
            flat: dict[str, Any] = dict(payload) if isinstance(payload, dict) else {}
            flat.update({"event": record.get("event"), "ts": record.get("ts"), "seq": record.get("seq")})
            record = flat
        events.append(record)
    return events


# --- verification ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainPoint:
    """A verified position in a chain: the entry ``seq`` with ``hash`` ends at byte ``offset``."""

    offset: int
    seq: int
    hash: str


@dataclass
class AuditVerifyReport:
    path: str
    ok: bool
    entries: int = 0
    legacy_prefix_bytes: int = 0
    last: Optional[ChainPoint] = None
    file_bytes: int = 0
    error: Optional[str] = None
    error_line: Optional[int] = None
    incremental: bool = False
    points: dict[int, ChainPoint] = field(default_factory=dict, repr=False)

    def summary(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "ok": self.ok,
            "entries": self.entries,
            "legacy_prefix_bytes": self.legacy_prefix_bytes,
            "last_seq": self.last.seq if self.last else None,
            "last_hash": self.last.hash if self.last else None,
            "last_offset": self.last.offset if self.last else None,
            "file_bytes": self.file_bytes,
            "incremental": self.incremental,
            "error": self.error,
            "error_line": self.error_line,
        }


def verify_audit_log(
    path: Path,
    raw_key: bytes,
    *,
    start: Optional[ChainPoint] = None,
    want_points: Optional[set[int]] = None,
) -> AuditVerifyReport:
    """Verify a whole audit log, or (``start``) only what was appended after a verified point."""
    path = Path(path).expanduser()
    report = AuditVerifyReport(path=str(path), ok=False, incremental=start is not None)
    chain_key = derive_chain_key(raw_key)
    key_id = key_id_for(chain_key)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        report.error = "audit log does not exist"
        return report
    report.file_bytes = len(data)
    if data and not data.endswith(b"\n"):
        report.error = "audit log ends with a partial line"
        return report

    if start is not None:
        if start.offset > len(data):
            report.error = "audit log is shorter than the last verified point (truncated or replaced)"
            return report
        line_start = data.rfind(b"\n", 0, max(0, start.offset - 1)) + 1
        anchor_line = data[line_start : start.offset - 1] if start.offset else b""
        try:
            anchor = json.loads(anchor_line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            anchor = None
        if (
            data[start.offset - 1 : start.offset] != b"\n"
            or not isinstance(anchor, dict)
            or anchor.get("seq") != start.seq
            or anchor.get("hash") != start.hash
            or _check_entry(anchor, chain_key, key_id) is not None
        ):
            report.error = "entry at the last verified point changed (log rewritten)"
            return report
        offset, previous, expected_seq, line_no = start.offset, start.hash, start.seq + 1, None
        report.last = start
    else:
        first = _first_chained_offset(data)
        if first is None:
            report.error = "audit log has no keyed chain entries" if data else "audit log is empty"
            return report
        report.legacy_prefix_bytes = first
        offset, previous, expected_seq, line_no = first, ZERO_HASH, 0, data.count(b"\n", 0, first)

    for raw in data[offset:].splitlines(keepends=True):
        line_no = (line_no or 0) + 1 if line_no is not None else None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            parsed = None
        reason = _check_entry(parsed, chain_key, key_id)
        entry: dict[str, Any] = parsed if isinstance(parsed, dict) else {}
        if reason is None and entry["seq"] != expected_seq:
            reason = f"sequence number {entry['seq']} where {expected_seq} was expected (entries removed or reordered)"
        if reason is None and entry["previous_hash"] != previous:
            reason = "previous_hash does not match the preceding entry"
        if reason is None and expected_seq == 0:
            reason = _check_genesis(entry, data[:offset])
        if reason is None and expected_seq > 0 and entry["event"] == GENESIS_EVENT:
            reason = "a second genesis entry inside the chain"
        if reason is not None:
            report.error = reason
            report.error_line = line_no
            return report
        offset += len(raw)
        previous = entry["hash"]
        point = ChainPoint(offset, entry["seq"], entry["hash"])
        if want_points and point.seq in want_points:
            report.points[point.seq] = point
        report.last = point
        report.entries += 1
        expected_seq += 1

    report.ok = True
    return report


def _check_genesis(entry: Mapping[str, Any], prefix: bytes) -> Optional[str]:
    if entry.get("event") != GENESIS_EVENT:
        return "the chain does not start with a genesis entry"
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return "genesis payload is missing"
    if payload.get("prefix_bytes") != len(prefix) or payload.get("prefix_sha256") != _sha256(prefix):
        return "the lines before the chain changed since it started"
    return None


# --- rotation --------------------------------------------------------------------------------


def rotate(path: Path, *, reason: str, key: bytes, previous_key: Optional[bytes] = None) -> dict[str, Any]:
    """Seal the current log under a new name and start a new chain that commits to it.

    Use it to recover from a corrupt log (after keeping it as evidence) or to rotate
    the audit key; ``previous_key`` verifies the sealed log when the key changed.
    """
    path = Path(path).expanduser()
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("a rotation reason is required")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    sealed = path.with_name(f"{path.name}.{stamp}.{secrets.token_hex(4)}.sealed")
    with _exclusive_lock(path):
        if not path.exists():
            raise FileNotFoundError(f"audit log does not exist: {path}")
        os.replace(path, sealed)
        data = sealed.read_bytes()
        report = verify_audit_log(sealed, previous_key if previous_key is not None else key)
        genesis_payload = {
            "reason": "rotation",
            "rotation_reason": reason,
            "sealed_file": sealed.name,
            "sealed_sha256": _sha256(data),
            "sealed_bytes": len(data),
            "sealed_verification": report.summary(),
            **_prefix_summary(b""),
        }
        chain = AuditChain(path, key=key)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
        try:
            chain._write_genesis(fd, chain.chain_key(), genesis_payload)
        finally:
            os.close(fd)
    return {"sealed_file": str(sealed), "sealed_verification": report.summary(), "new_log": str(path)}


# --- periodic verification -------------------------------------------------------------------


class AuditIntegrityMonitor:
    """Re-verify audit logs on a schedule; on failure report it so callers fail closed.

    The first check verifies each log completely; later checks verify only what was
    appended since the last verified point, after confirming that point is unchanged.
    """

    def __init__(
        self,
        logs: list[Path],
        *,
        key_env: str = DEFAULT_KEY_ENV,
        interval_seconds: float = 300.0,
        full_interval_seconds: float = 3600.0,
        on_failure: Optional[Callable[[str], None]] = None,
        metrics: Any = None,
        environ: Optional[Mapping[str, str]] = None,
    ):
        self.logs = [Path(p).expanduser() for p in dict.fromkeys(str(p) for p in logs)]
        self.key_env = key_env
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.full_interval_seconds = max(self.interval_seconds, float(full_interval_seconds))
        self._last_full_at: Optional[float] = None
        self.on_failure = on_failure
        self.metrics = metrics
        self._environ = environ
        self._points: dict[str, ChainPoint] = {}
        self.failure: Optional[str] = None
        self.last_reports: list[dict[str, Any]] = []
        self.last_checked_at: Optional[float] = None

    def check_once(self) -> bool:
        reports = []
        now = time.monotonic()
        if self._last_full_at is None or now - self._last_full_at >= self.full_interval_seconds:
            # Incremental checks skip what was verified before; a periodic full
            # pass also catches edits inside the already-verified part.
            self._points.clear()
            self._last_full_at = now
        for log in self.logs:
            if not log.exists():
                continue
            try:
                key = load_audit_key(self.key_env, dev_key_file=dev_key_file_for(log), environ=self._environ)
                report = verify_audit_log(log, key, start=self._points.get(str(log)))
            except Exception as exc:  # an unreadable log is a failure too, never a crashed monitor
                report = AuditVerifyReport(path=str(log), ok=False, error=f"{type(exc).__name__}: {exc}")
            reports.append(report.summary())
            if report.ok and report.last is not None:
                self._points[str(log)] = report.last
            elif not report.ok:
                self._fail(f"audit log failed verification: {log}: {report.error}")
        self.last_reports = reports
        self.last_checked_at = time.time()
        self._metric("omnidesk_audit_chain_verifications_total", status="ok" if self.failure is None else "failed")
        return self.failure is None

    def _fail(self, message: str) -> None:
        self._metric("omnidesk_audit_chain_verify_failures_total")
        if self.failure is None:
            self.failure = message
            if self.on_failure is not None:
                self.on_failure(message)

    def _metric(self, name: str, **labels: Any) -> None:
        inc = getattr(self.metrics, "inc", None)
        if callable(inc):
            inc(name, **labels)

    async def run(self) -> None:
        while True:
            await asyncio.to_thread(self.check_once)
            await asyncio.sleep(self.interval_seconds)


# --- command line ----------------------------------------------------------------------------


def _key_from_env(name: str, log: Path) -> bytes:
    return load_audit_key(name, dev_key_file=dev_key_file_for(log))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omnidesk_agent.security.audit_chain", description="Verify or rotate OmniDesk keyed audit logs.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    verify_p = sub.add_parser("verify", help="verify one or more audit logs completely")
    verify_p.add_argument("logs", nargs="+", type=Path)
    verify_p.add_argument("--key-env", default=DEFAULT_KEY_ENV)
    verify_p.add_argument("--checkpoint-dir", type=Path, help="also check the latest signed checkpoint and write a new one")
    rotate_p = sub.add_parser("rotate", help="seal a log and start a new chain that commits to it")
    rotate_p.add_argument("log", type=Path)
    rotate_p.add_argument("--reason", required=True)
    rotate_p.add_argument("--key-env", default=DEFAULT_KEY_ENV)
    rotate_p.add_argument("--previous-key-env", help="environment variable holding the key the sealed log was written with")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "rotate":
            key = _key_from_env(args.key_env, args.log)
            previous = load_audit_key(args.previous_key_env) if args.previous_key_env else None
            print(json.dumps(rotate(args.log, reason=args.reason, key=key, previous_key=previous), indent=2, sort_keys=True))
            return 0
        ok = True
        results = []
        for log in args.logs:
            key = _key_from_env(args.key_env, log)
            if args.checkpoint_dir is not None:
                from omnidesk_agent.security.audit_worm import WormAuditCheckpoint

                checkpoints = WormAuditCheckpoint(args.checkpoint_dir, hmac_key_env=args.key_env)
                result = checkpoints.verify_and_anchor(log, key)
            else:
                result = verify_audit_log(log, key).summary()
            ok = ok and bool(result.get("ok"))
            results.append(result)
        print(json.dumps({"ok": ok, "logs": results}, indent=2, sort_keys=True))
        return 0 if ok else 1
    except AuditIntegrityError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
