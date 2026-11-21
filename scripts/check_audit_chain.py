#!/usr/bin/env python3
"""CI gate for the keyed audit chain verifier.

Writes a log through the real runtime writers (permission gate, break-glass store,
admin auth), runs the verifier command on it, then damages copies of it the ways
an attacker would and requires the verifier to reject every one. The log is
generated here on the CI runner; this checks the verifier, not a production log.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

KEY_ENV = "OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY"


def _verify(logs: list[Path], env: dict[str, str], checkpoint_dir: Path | None = None) -> int:
    command = [sys.executable, "-m", "omnidesk_agent.security.audit_chain", "verify", *map(str, logs)]
    if checkpoint_dir is not None:
        command += ["--checkpoint-dir", str(checkpoint_dir)]
    return subprocess.run(command, env=env, cwd=REPO_ROOT, capture_output=True, text=True, timeout=120).returncode


def _write_runtime_log(workdir: Path) -> tuple[Path, Path]:
    from omnidesk_agent.config import PermissionConfig
    from omnidesk_agent.security.admin_auth import AdminAuth
    from omnidesk_agent.security.break_glass import BreakGlassStore
    from omnidesk_agent.security.permissions import PermissionManager

    audit = workdir / "audit.log"
    admin = workdir / "admin_auth_audit.jsonl"
    pm = PermissionManager(PermissionConfig(default_mode="allow", audit_log=audit))
    for i in range(5):
        pm.verify({"tool": "files", "action": f"list-{i}", "risk": "low", "source": "local-cli", "actor": "owner"})
    store = BreakGlassStore(workdir / "break_glass.sqlite3", audit_log=audit)
    store.open(session_id="ci-drill", actor="owner", approved_by="owner", reason="CI verifier drill", ttl_seconds=60)
    store.revoke("ci-drill", revoked_by="owner")
    pm.record_policy_block({"tool": "files", "action": "write_text", "risk": "high", "args": {"path": "../x"}}, rule="path_escape", reason="CI drill")
    auth = AdminAuth(audit_log=admin)
    auth.verify_headers({"authorization": "Bearer wrong"}, "127.0.0.1", path="/admin/status")
    return audit, admin


def _damaged_copies(audit: Path, workdir: Path) -> dict[str, Path]:
    lines = audit.read_text(encoding="utf-8").splitlines(keepends=True)
    cases: dict[str, list[str]] = {}
    edited = json.loads(lines[3])
    edited["payload"]["actor"] = "mallory"
    cases["edited_entry"] = [*lines[:3], json.dumps(edited, sort_keys=True) + "\n", *lines[4:]]
    cases["deleted_entry"] = [*lines[:3], *lines[4:]]
    cases["reordered_entries"] = [*lines[:3], lines[4], lines[3], *lines[5:]]
    cases["unchained_line"] = [*lines, json.dumps({"event": "break_glass.open", "ts": 0}) + "\n"]
    cases["partial_line"] = [*lines, '{"event": "half']
    out = {}
    for name, content in cases.items():
        path = workdir / f"{name}.log"
        path.write_text("".join(content), encoding="utf-8")
        out[name] = path
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write a JSON report here")
    args = parser.parse_args(argv)

    workdir = Path(tempfile.mkdtemp(prefix="omnidesk-audit-chain-"))
    env = {**os.environ, KEY_ENV: os.environ.get(KEY_ENV) or "ci-audit-chain-drill-key-" + "c" * 32, "PYTHONPATH": str(REPO_ROOT)}
    os.environ[KEY_ENV] = env[KEY_ENV]
    report: dict[str, object] = {"ok": False, "checks": {}}
    checks: dict[str, bool] = report["checks"]  # type: ignore[assignment]
    try:
        audit, admin = _write_runtime_log(workdir)
        anchors = workdir / "anchors"
        checks["runtime_logs_verify"] = _verify([audit, admin], env) == 0
        checks["anchor_written"] = _verify([audit], env, anchors) == 0
        for name, path in _damaged_copies(audit, workdir).items():
            checks[f"rejects_{name}"] = _verify([path], env) == 1
        truncated = workdir / "audit.log"
        lines = truncated.read_text(encoding="utf-8").splitlines(keepends=True)
        truncated.write_text("".join(lines[:-1]), encoding="utf-8")
        checks["valid_prefix_still_verifies"] = _verify([truncated], env) == 0
        checks["rejects_truncation_against_anchor"] = _verify([truncated], env, anchors) == 1
        checks["rejects_wrong_key"] = _verify([admin], {**env, KEY_ENV: "some-other-key-" + "w" * 32}) == 1
        report["ok"] = all(checks.values())
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
