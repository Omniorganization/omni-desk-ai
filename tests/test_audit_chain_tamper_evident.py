"""The security audit log is a keyed hash chain that detects tampering and fails closed.

Regression tests for the 2026-11-21 security audit (high): the SHA-256 chain had
no key and no verifier, restarted from zero after a malformed last line, and
break-glass wrote unchained lines into the same file. Each test attacks the log
the way the audit describes and must fail if the weakness comes back.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from omnidesk_agent.config import PermissionConfig
from omnidesk_agent.repositories.postgres_state import PostgresBreakGlassStore
from omnidesk_agent.security.admin_auth import AdminAuth
from omnidesk_agent.security.audit_chain import (
    CHAIN_FORMAT,
    AuditChain,
    AuditIntegrityError,
    AuditIntegrityMonitor,
    load_audit_key,
    read_audit_events,
    rotate,
    verify_audit_log,
)
from omnidesk_agent.security.audit_worm import WormAuditCheckpoint
from omnidesk_agent.security.break_glass import BreakGlassStore
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.validation.production import validate_production_config

KEY_ENV = "OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY"
KEY = b"audit-chain-test-key-" + b"z" * 32
REPO_ROOT = Path(__file__).resolve().parents[1]


def _low(**extra):
    return {"tool": "files", "action": "list", "risk": "low", "source": "local-cli", "actor": "owner", **extra}


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def _manager(tmp_path: Path, monkeypatch) -> PermissionManager:
    monkeypatch.setenv(KEY_ENV, KEY.decode())
    return PermissionManager(PermissionConfig(default_mode="allow", audit_log=tmp_path / "audit.log"))


def test_entries_from_every_writer_form_one_verifiable_chain(tmp_path, monkeypatch):
    pm = _manager(tmp_path, monkeypatch)
    pm.verify(_low())
    BreakGlassStore(tmp_path / "bg.sqlite3", audit_log=pm.audit_log).open(session_id="s1", actor="owner", approved_by="owner", reason="outage", ttl_seconds=60)
    pm.verify(_low(action="list2"))
    PostgresBreakGlassStore(_MemoryState(), audit_log=pm.audit_log).open(session_id="s2", actor="owner", approved_by="owner", reason="outage", ttl_seconds=60)
    pm.verify(_low(action="list3"))

    report = verify_audit_log(pm.audit_log, KEY)

    assert report.ok, report.error
    entries = [json.loads(line) for line in _lines(pm.audit_log)]
    assert all(entry["chain"] == CHAIN_FORMAT for entry in entries)
    # Break-glass no longer resets the chain: every entry links to its predecessor.
    assert [entry["seq"] for entry in entries] == list(range(len(entries)))
    assert all(entries[i]["previous_hash"] == entries[i - 1]["hash"] for i in range(1, len(entries)))
    assert "break_glass.open" in [entry["event"] for entry in entries]


def test_rewriting_an_entry_is_detected_even_when_its_hash_is_recomputed(tmp_path, monkeypatch):
    pm = _manager(tmp_path, monkeypatch)
    for i in range(4):
        pm.verify(_low(action=f"a{i}"))
    lines = _lines(pm.audit_log)
    entry = json.loads(lines[2])
    entry["payload"]["actor"] = "mallory"
    # The old unkeyed chain could be rebuilt by anyone: try exactly that.
    body = {k: v for k, v in entry.items() if k != "hash"}
    entry["hash"] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    lines[2] = json.dumps(entry, sort_keys=True)
    _write_lines(pm.audit_log, lines)

    report = verify_audit_log(pm.audit_log, KEY)

    assert not report.ok
    assert report.error_line == 3
    assert "HMAC" in report.error


@pytest.mark.parametrize("attack", ["delete_middle", "swap", "duplicate"])
def test_removing_reordering_or_replaying_entries_is_detected(tmp_path, monkeypatch, attack):
    pm = _manager(tmp_path, monkeypatch)
    for i in range(5):
        pm.verify(_low(action=f"a{i}"))
    lines = _lines(pm.audit_log)
    if attack == "delete_middle":
        del lines[2]
    elif attack == "swap":
        lines[2], lines[3] = lines[3], lines[2]
    else:
        lines.insert(3, lines[2])
    _write_lines(pm.audit_log, lines)

    assert not verify_audit_log(pm.audit_log, KEY).ok


def test_cutting_entries_off_the_end_is_caught_by_a_signed_anchor(tmp_path, monkeypatch):
    pm = _manager(tmp_path, monkeypatch)
    for i in range(4):
        pm.verify(_low(action=f"a{i}"))
    anchors = WormAuditCheckpoint(tmp_path / "anchors")
    assert anchors.verify_and_anchor(pm.audit_log, KEY)["ok"]
    _write_lines(pm.audit_log, _lines(pm.audit_log)[:-2])

    # A shorter chain is still internally consistent...
    assert verify_audit_log(pm.audit_log, KEY).ok
    # ...but no longer holds the entry the last check anchored.
    result = anchors.verify_and_anchor(pm.audit_log, KEY)
    assert not result["ok"]
    assert "anchored entry" in result["error"]


def test_a_forged_anchor_is_rejected(tmp_path, monkeypatch):
    pm = _manager(tmp_path, monkeypatch)
    pm.verify(_low())
    anchors = WormAuditCheckpoint(tmp_path / "anchors")
    anchors.verify_and_anchor(pm.audit_log, KEY)
    anchor_file = next((tmp_path / "anchors").glob("audit-anchor-*.json"))
    record = json.loads(anchor_file.read_text())
    record["seq"] = 0
    anchor_file.write_text(json.dumps(record))

    result = anchors.verify_and_anchor(pm.audit_log, KEY)

    assert not result["ok"]
    assert "signature" in result["error"]


@pytest.mark.parametrize("damage", ["partial_line", "not_json", "unchained_line", "other_key"])
def test_writers_fail_closed_instead_of_restarting_the_chain(tmp_path, monkeypatch, damage):
    pm = _manager(tmp_path, monkeypatch)
    pm.verify(_low())
    if damage == "partial_line":
        with pm.audit_log.open("a") as handle:
            handle.write('{"event": "half')
    elif damage == "not_json":
        with pm.audit_log.open("a") as handle:
            handle.write("not json\n")
    elif damage == "unchained_line":
        # What break-glass used to append.
        with pm.audit_log.open("a") as handle:
            handle.write(json.dumps({"event": "break_glass.open", "ts": 1}) + "\n")
    else:
        # A well-formed chain entry, but under a key the gateway does not hold.
        other = tmp_path / "other.log"
        AuditChain(other, key=b"another-key-" + b"q" * 32).append("x", {})
        with pm.audit_log.open("a") as handle:
            handle.write(_lines(other)[-1] + "\n")
    before = pm.audit_log.read_bytes()

    with pytest.raises(PermissionError):
        pm.verify(_low(action="next"))

    assert pm.audit_log.read_bytes() == before  # nothing appended, no new genesis


def test_a_tool_does_not_run_when_its_audit_entry_cannot_be_written(tmp_path, monkeypatch):
    import asyncio

    from omnidesk_agent.tools.base import ToolContext
    from omnidesk_agent.tools.files import FilesTool

    pm = _manager(tmp_path, monkeypatch)
    pm.verify(_low())
    with pm.audit_log.open("a") as handle:
        handle.write("garbage\n")
    workspace = tmp_path / "ws"

    with pytest.raises(PermissionError):
        asyncio.run(FilesTool(workspace, allow_write=True).call("write_text", {"path": "out.txt", "text": "x"}, ToolContext(permissions=pm)))

    assert not (workspace / "out.txt").exists()


def test_legacy_log_is_upgraded_and_its_old_lines_are_protected(tmp_path, monkeypatch):
    log = tmp_path / "audit.log"
    legacy = [json.dumps({"event": "proposal", "hash": "a" * 64, "previous_hash": "0" * 64}), json.dumps({"event": "break_glass.open"})]
    _write_lines(log, legacy)
    pm = _manager(tmp_path, monkeypatch)

    pm.verify(_low())

    report = verify_audit_log(log, KEY)
    assert report.ok and report.legacy_prefix_bytes == len(("\n".join(legacy) + "\n").encode())
    _write_lines(log, [legacy[0].replace("proposal", "rewritten"), *_lines(log)[1:]])
    assert "before the chain" in verify_audit_log(log, KEY).error


def test_rotation_seals_a_damaged_log_and_starts_a_chain_that_commits_to_it(tmp_path, monkeypatch):
    pm = _manager(tmp_path, monkeypatch)
    pm.verify(_low())
    with pm.audit_log.open("a") as handle:
        handle.write("garbage\n")

    result = rotate(pm.audit_log, reason="recover from damaged tail", key=KEY)

    sealed = Path(result["sealed_file"])
    assert sealed.exists() and result["sealed_verification"]["ok"] is False
    genesis = json.loads(_lines(pm.audit_log)[0])
    assert genesis["payload"]["sealed_sha256"] == hashlib.sha256(sealed.read_bytes()).hexdigest()
    assert genesis["payload"]["rotation_reason"] == "recover from damaged tail"
    fresh = PermissionManager(PermissionConfig(default_mode="allow", audit_log=pm.audit_log))
    assert fresh.verify(_low(action="after")).allowed
    assert verify_audit_log(pm.audit_log, KEY).ok


def test_production_never_falls_back_to_a_local_key(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.setenv("OMNIDESK_ENV", "production")
    pm = PermissionManager(PermissionConfig(default_mode="allow", audit_log=tmp_path / "audit.log"))

    with pytest.raises(AuditIntegrityError, match="not configured"):
        pm.verify(_low())

    assert not (tmp_path / "audit.log.key").exists()
    issues = validate_production_config(__import__("omnidesk_agent.config", fromlist=["AppConfig"]).AppConfig(), {"OMNIDESK_ENV": "production"})["issues"]
    assert f"audit checkpoint HMAC key is not configured: {KEY_ENV}" in issues


def test_development_generates_a_private_per_log_key(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.delenv("OMNIDESK_ENV", raising=False)
    pm = PermissionManager(PermissionConfig(default_mode="allow", audit_log=tmp_path / "audit.log"))

    pm.verify(_low())

    key_file = tmp_path / "audit.log.key"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert verify_audit_log(pm.audit_log, load_audit_key(KEY_ENV, dev_key_file=key_file)).ok
    assert not verify_audit_log(pm.audit_log, KEY).ok


def test_monitor_failure_makes_every_decision_fail_closed(tmp_path, monkeypatch):
    pm = _manager(tmp_path, monkeypatch)
    for i in range(3):
        pm.verify(_low(action=f"a{i}"))
    monitor = AuditIntegrityMonitor([pm.audit_log], on_failure=pm.mark_audit_integrity_failure)
    assert monitor.check_once() is True
    lines = _lines(pm.audit_log)
    entry = json.loads(lines[1])
    entry["payload"]["action"] = "hidden"
    lines[1] = json.dumps(entry, sort_keys=True)
    _write_lines(pm.audit_log, lines)

    assert monitor.check_once() is False

    assert "audit log failed verification" in str(monitor.failure)
    with pytest.raises(AuditIntegrityError):
        pm.verify(_low(action="low risk is refused too"))
    with pytest.raises(AuditIntegrityError):
        pm.record_access(_low())


def test_monitor_checks_incrementally_and_catches_a_rewrite_of_the_verified_part(tmp_path, monkeypatch):
    pm = _manager(tmp_path, monkeypatch)
    pm.verify(_low())
    monitor = AuditIntegrityMonitor([pm.audit_log], full_interval_seconds=10_000)
    assert monitor.check_once()
    pm.verify(_low(action="new"))
    assert monitor.check_once()
    assert monitor.last_reports[0]["incremental"] is True
    _write_lines(pm.audit_log, _lines(pm.audit_log)[:-1])

    assert monitor.check_once() is False


def test_policy_blocks_and_ungated_access_are_chained_audit_events(tmp_path, monkeypatch):
    pm = _manager(tmp_path, monkeypatch)

    pm.record_access({**_low(), "args": {"path": "."}})
    pm.record_policy_block({"tool": "files", "action": "write_text", "risk": "high", "args": {"path": "../escape"}, "reason": "write"}, rule="path_escape", reason="path escapes workspace")

    events = read_audit_events(pm.audit_log)
    assert [event["event"] for event in events] == ["access_without_approval", "policy_blocked"]
    assert events[1]["block_rule"] == "path_escape" and events[1]["reason"] == "write"
    assert verify_audit_log(pm.audit_log, KEY).ok


def test_break_glass_is_refused_before_any_change_when_the_log_is_damaged(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY.decode())
    log = tmp_path / "audit.log"
    store = BreakGlassStore(tmp_path / "bg.sqlite3", audit_log=log)
    store.open(session_id="ok", actor="owner", approved_by="owner", reason="outage", ttl_seconds=60)
    with log.open("a") as handle:
        handle.write("garbage\n")

    with pytest.raises(AuditIntegrityError):
        store.open(session_id="s1", actor="owner", approved_by="owner", reason="outage", ttl_seconds=60)
    with pytest.raises(KeyError):
        store.get("s1")


def test_a_break_glass_session_whose_audit_write_failed_is_revoked(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY.decode())
    store = BreakGlassStore(tmp_path / "bg.sqlite3", audit_log=tmp_path / "audit.log")
    store.audit_chain.check()

    def fail(*_args, **_kwargs):
        raise AuditIntegrityError("disk full")

    monkeypatch.setattr(store.audit_chain, "check", lambda: (0, "0" * 64))
    monkeypatch.setattr(store.audit_chain, "append", fail)
    with pytest.raises(AuditIntegrityError):
        store.open(session_id="s1", actor="owner", approved_by="owner", reason="outage", ttl_seconds=60)

    assert store.get("s1").active is False


def test_admin_requests_are_refused_when_their_audit_log_is_damaged(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY.decode())
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "o" * 40)
    audit = tmp_path / "admin.jsonl"
    auth = AdminAuth(audit_log=audit)
    assert auth.verify_headers({"authorization": "Bearer " + "o" * 40}, "127.0.0.1", required_role="owner").ok
    with audit.open("a") as handle:
        handle.write("garbage\n")

    decision = auth.verify_headers({"authorization": "Bearer " + "o" * 40}, "127.0.0.1", required_role="owner")

    assert not decision.ok
    assert "admin audit log unavailable" in decision.reason


def _append_many(path: str, key: bytes, worker: int) -> None:
    chain = AuditChain(Path(path), key=key)
    for i in range(40):
        chain.append("concurrent", {"worker": worker, "i": i})


def test_concurrent_writers_in_threads_and_processes_keep_one_chain(tmp_path):
    log = tmp_path / "audit.log"
    threads = [threading.Thread(target=_append_many, args=(str(log), KEY, n)) for n in range(3)]
    ctx = multiprocessing.get_context("spawn")
    processes = [ctx.Process(target=_append_many, args=(str(log), KEY, 10 + n)) for n in range(2)]
    for worker in (*threads, *processes):
        worker.start()
    for worker in (*threads, *processes):
        worker.join(timeout=120)
    assert all(p.exitcode == 0 for p in processes)

    report = verify_audit_log(log, KEY)

    assert report.ok, report.error
    assert report.entries == 1 + 5 * 40


def test_verifier_command_exit_codes_and_checkpoint_dir(tmp_path, monkeypatch):
    pm = _manager(tmp_path, monkeypatch)
    for i in range(3):
        pm.verify(_low(action=f"a{i}"))
    env = {**os.environ, KEY_ENV: KEY.decode(), "PYTHONPATH": str(REPO_ROOT)}
    command = [sys.executable, "-m", "omnidesk_agent.security.audit_chain", "verify", str(pm.audit_log), "--checkpoint-dir", str(tmp_path / "anchors")]

    first = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["ok"] is True

    _write_lines(pm.audit_log, _lines(pm.audit_log)[:-1])
    truncated = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    assert truncated.returncode == 1

    no_key = subprocess.run(command[:5], env={**env, KEY_ENV: "", "OMNIDESK_ENV": "production"}, capture_output=True, text=True, timeout=60)
    assert no_key.returncode == 2


class _MemoryState:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, dict]] = {}

    def insert_once(self, namespace, key, value) -> bool:
        bucket = self.rows.setdefault(namespace, {})
        if key in bucket:
            return False
        bucket[key] = dict(value)
        return True

    def get(self, namespace, key):
        row = self.rows.get(namespace, {}).get(key)
        return dict(row) if row is not None else None

    def update_locked(self, namespace, key, updater):
        current = self.get(namespace, key)
        if current is None:
            raise KeyError(key)
        updated = updater(current)
        self.rows[namespace][key] = dict(updated)
        return updated


def test_agent_loop_events_are_chained_and_cannot_pose_as_decisions(tmp_path, monkeypatch):
    pm = _manager(tmp_path, monkeypatch)

    pm.record_event("agent_step_finished", {"step_index": 2, "tool_action": "files.read_text", "ok": True, "token": "abc"})

    event = read_audit_events(pm.audit_log)[-1]
    assert event["event"] == "agent_step_finished" and event["token"] == "[REDACTED]"
    for bad in ("allowed_default", "approval_required", "agent-x", "agent_" + "x" * 70):
        with pytest.raises(ValueError):
            pm.record_event(bad, {})
    pm.mark_audit_integrity_failure("tampered")
    with pytest.raises(AuditIntegrityError):
        pm.record_event("agent_run_finished", {"status": "completed"})


def test_runtime_keeps_admin_audit_and_checkpoints_out_of_the_workspace(tmp_path):
    from omnidesk_agent.config import AppConfig
    from omnidesk_agent.daemon import OmniDeskRuntime

    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "workspace"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit" / "audit.log"
    cfg.learning.growth_plan_file = tmp_path / "growth.json"
    cfg.channels.gmail.credentials_file = tmp_path / "g" / "c.json"
    cfg.channels.gmail.token_file = tmp_path / "g" / "t.json"
    cfg.plugins.enabled = False
    cfg.ensure_dirs()
    rt = OmniDeskRuntime(cfg)
    try:
        assert rt.admin_auth.audit_log == tmp_path / "audit" / "admin_auth_audit.jsonl"
        assert rt.audit_checkpoint.checkpoint_dir == tmp_path / "audit" / "audit_checkpoints"
        assert rt.admin_auth.audit_chain is not None and rt.admin_auth.audit_chain.key_env == cfg.permissions.audit_checkpoint_hmac_key_env
    finally:
        rt.close()


def test_verify_and_rotate_commands_in_process(tmp_path, monkeypatch, capsys):
    from omnidesk_agent.security import audit_chain

    pm = _manager(tmp_path, monkeypatch)
    pm.verify(_low())
    assert audit_chain.main(["verify", str(pm.audit_log)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    with pm.audit_log.open("a") as handle:
        handle.write("garbage\n")
    assert audit_chain.main(["verify", str(pm.audit_log)]) == 1
    capsys.readouterr()

    assert audit_chain.main(["rotate", str(pm.audit_log), "--reason", "drill"]) == 0
    rotated = json.loads(capsys.readouterr().out)
    assert Path(rotated["sealed_file"]).exists() and rotated["sealed_verification"]["ok"] is False
    assert audit_chain.main(["verify", str(pm.audit_log), "--checkpoint-dir", str(tmp_path / "anchors")]) == 0
    capsys.readouterr()

    monkeypatch.delenv(KEY_ENV)
    monkeypatch.setenv("OMNIDESK_ENV", "production")
    assert audit_chain.main(["verify", str(pm.audit_log)]) == 2


def test_rotate_requires_a_reason_and_an_existing_log(tmp_path):
    with pytest.raises(ValueError, match="reason"):
        rotate(tmp_path / "audit.log", reason=" ", key=KEY)
    with pytest.raises(FileNotFoundError):
        rotate(tmp_path / "missing.log", reason="x", key=KEY)


def test_verifier_reports_missing_empty_and_unchained_logs(tmp_path):
    assert verify_audit_log(tmp_path / "missing.log", KEY).error == "audit log does not exist"
    (tmp_path / "empty.log").write_text("")
    assert verify_audit_log(tmp_path / "empty.log", KEY).error == "audit log is empty"
    (tmp_path / "legacy.log").write_text('{"event": "old"}\n')
    assert verify_audit_log(tmp_path / "legacy.log", KEY).error == "audit log has no keyed chain entries"
    log = tmp_path / "chain.log"
    chain = AuditChain(log, key=KEY)
    chain.append("a", {})
    report = verify_audit_log(log, KEY)
    _write_lines(log, [])
    assert "shorter than the last verified point" in verify_audit_log(log, KEY, start=report.last).error


def test_monitor_treats_an_unreadable_log_as_a_failure(tmp_path, monkeypatch):
    pm = _manager(tmp_path, monkeypatch)
    pm.verify(_low())
    failures: list[str] = []
    monitor = AuditIntegrityMonitor([pm.audit_log], on_failure=failures.append)
    monkeypatch.setattr("omnidesk_agent.security.audit_chain.verify_audit_log", lambda *a, **k: (_ for _ in ()).throw(OSError("I/O error")))

    assert monitor.check_once() is False

    assert failures and "OSError" in failures[0]


def test_everything_that_boots_production_passes_the_audit_key():
    """Production refuses to start without the key, so every shipped production boot path must pass it."""
    for path in ("deploy/docker/docker-compose.yml", "deploy/docker/docker-compose.full.yml"):
        assert "OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY: " in (REPO_ROOT / path).read_text(encoding="utf-8"), path
    for script in ("scripts/isolated_cloud_acceptance.py", "scripts/web_first_host.py", "scripts/init_production_config.py"):
        assert "AUDIT_CHECKPOINT_HMAC_KEY" in (REPO_ROOT / script).read_text(encoding="utf-8"), script


def test_a_failed_revoke_never_hides_the_audit_failure():
    # gateway-clients' review: a revoke of a missing row raised KeyError and
    # would have replaced the AuditIntegrityError the caller has to see.
    from omnidesk_agent.security.audit_errors import AuditIntegrityError
    from omnidesk_agent.security.break_glass import audit_transition

    class BrokenChain:
        def append(self, event, fields):
            raise AuditIntegrityError("audit log cannot be written")

    revoked: list[str] = []

    def revoke_missing_row(session_id: str) -> None:
        revoked.append(session_id)
        raise KeyError(session_id)

    with pytest.raises(AuditIntegrityError):
        audit_transition(BrokenChain(), "break_glass.open", {"session_id": "bg-1"}, revoke=revoke_missing_row)  # type: ignore[arg-type]

    assert revoked == ["bg-1"]  # the revoke was still attempted
