"""Out-of-process watchdog for failures that stop the gateway itself.

    python -m omnidesk_agent.self_healing.watchdog --health-url URL --config PATH \\
        --state-dir DIR --restart-cmd systemctl --restart-cmd restart --restart-cmd omnidesk-agent

Each check GETs the health URL. After ``--failures`` consecutive failures the
watchdog restarts the gateway and waits for health. It never changes the
config: the config holds security, sandbox and approval settings, so going
back to an older one is an owner's decision. When the config on disk differs
from the last known-good snapshot the record says so, and an operator can put
the snapshot back with ``--restore-known-good``.

A config becomes known-good only after this watchdog restarted the gateway
with it on disk and the gateway then stayed healthy for ``--good-after``
seconds. The gateway reads its config only at start, so a file written while
the old process keeps serving proves nothing. Restarts are limited per hour;
past the limit the watchdog only records an escalation. The restart command is
a fixed argv from the command line (no shell). Every step is appended to
``<state-dir>/watchdog.jsonl``.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess  # nosec B404
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from omnidesk_agent.self_healing.probes import ConfigSnapshots, HttpHealthProbe


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


class GatewayWatchdog:
    def __init__(
        self,
        health: HttpHealthProbe,
        config_path: Path,
        state_dir: Path,
        *,
        restart: Callable[[], Awaitable[dict[str, Any]]],
        failures_before_action: int = 3,
        good_after_seconds: float = 300.0,
        max_restarts_per_hour: int = 4,
        recovery_timeout_seconds: float = 120.0,
        poll_seconds: float = 2.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.health = health
        self.config_path = Path(config_path).expanduser()
        self.state_dir = Path(state_dir).expanduser()
        self.snapshots = ConfigSnapshots(self.state_dir / "config_snapshots")
        self.audit_path = self.state_dir / "watchdog.jsonl"
        self.restart = restart
        self.failures_before_action = max(1, int(failures_before_action))
        self.good_after_seconds = float(good_after_seconds)
        self.max_restarts_per_hour = max(1, int(max_restarts_per_hour))
        self.recovery_timeout_seconds = float(recovery_timeout_seconds)
        self.poll_seconds = float(poll_seconds)
        self.clock = clock
        self.sleep = sleep
        self.consecutive_failures = 0
        self.healthy_since: Optional[float] = None
        # The config the running gateway loaded, when known: only a restart by
        # this watchdog tells. For a process it did not start it stays unknown.
        self.loaded_digest: Optional[str] = None
        self._restarts: deque[float] = deque()

    async def healthy(self) -> bool:
        return bool((await self.health.collect())[0].health_ok)

    def _read_config(self) -> Optional[bytes]:
        try:
            return self.config_path.read_bytes()
        except FileNotFoundError:
            return None

    async def check(self) -> dict[str, Any]:
        now = self.clock()
        if await self.healthy():
            self.consecutive_failures = 0
            if self.healthy_since is None:
                self.healthy_since = now
            data = self._read_config()
            digest = _digest(data) if data is not None else None
            if (
                data is not None
                and digest == self.loaded_digest
                and digest != self.snapshots.known_good()
                and now - self.healthy_since >= self.good_after_seconds
            ):
                self.snapshots.save_known_good(data)  # exactly the bytes that were compared
                return self._audit({"event": "known_good_saved", "snapshot": digest})
            return {"event": "healthy"}
        self.healthy_since = None
        self.consecutive_failures += 1
        if self.consecutive_failures < self.failures_before_action:
            return self._audit({"event": "unhealthy", "consecutive_failures": self.consecutive_failures})
        while self._restarts and now - self._restarts[0] > 3600:
            self._restarts.popleft()
        if len(self._restarts) >= self.max_restarts_per_hour:
            return self._audit({"event": "escalated", "reason": "restart limit reached; a human must intervene"})
        return await self._recover()

    async def _recover(self) -> dict[str, Any]:
        started = self.clock()
        known_good = self.snapshots.known_good()
        data = self._read_config()
        digest = _digest(data) if data is not None else None
        changed = None
        if known_good and digest != known_good:
            # Reported, never reverted here: restoring is an owner's call (--restore-known-good).
            changed = {"known_good": known_good, "current": digest}
        self._restarts.append(self.clock())
        self.loaded_digest = None
        try:
            restart = await self.restart()
        except Exception as exc:  # a failed restart must not stop the watchdog
            return self._audit({
                "event": "recovery_failed",
                "containment_action": "restart",
                "config_differs_from_known_good": changed,
                "restart": {"error_class": type(exc).__name__, "error": str(exc)[:300]},
            })
        if (restart or {}).get("exit_code", 0) == 0:
            self.loaded_digest = digest  # the restarted gateway read the file that is on disk now
        recovered = False
        while self.clock() - started < self.recovery_timeout_seconds:
            await self.sleep(self.poll_seconds)
            if await self.healthy():
                recovered = True
                break
        if recovered:
            self.consecutive_failures = 0
            self.healthy_since = self.clock()
        return self._audit({
            "event": "recovered" if recovered else "recovery_failed",
            "containment_action": "restart",
            "config_differs_from_known_good": changed,
            "restart": restart,
            "recovery_seconds": round(self.clock() - started, 3),
        })

    def _audit(self, record: dict[str, Any]) -> dict[str, Any]:
        record = {"at": self.clock(), **record}
        _append(self.audit_path, record)
        return record


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def restore_known_good(config_path: Path, state_dir: Path) -> dict[str, Any]:
    """Operator action: put the last known-good config back; the current file is kept."""
    state_dir = Path(state_dir).expanduser()
    snapshots = ConfigSnapshots(state_dir / "config_snapshots")
    known_good = snapshots.known_good()
    if not known_good:
        record: dict[str, Any] = {"at": time.time(), "event": "restore_refused", "reason": "no known-good snapshot"}
    else:
        previous = snapshots.restore(known_good, Path(config_path).expanduser())
        record = {"at": time.time(), "event": "restored_by_operator", "snapshot": known_good, "previous_config": str(previous)}
    _append(state_dir / "watchdog.jsonl", record)
    return record


def command_restarter(argv: list[str], *, timeout_seconds: float = 60.0) -> Callable[[], Awaitable[dict[str, Any]]]:
    if not argv:
        raise ValueError("restart command is required")

    async def restart() -> dict[str, Any]:
        def run() -> dict[str, Any]:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_seconds, check=False)  # nosec B603
            return {"argv": argv, "exit_code": proc.returncode, "stderr_tail": proc.stderr[-500:]}

        return await asyncio.to_thread(run)

    return restart


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omnidesk_agent.self_healing.watchdog")
    parser.add_argument("--health-url")
    parser.add_argument("--config", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--restart-cmd", action="append", help="one argv element per flag")
    parser.add_argument("--failures", type=int, default=3)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--good-after", type=float, default=300.0)
    parser.add_argument("--max-restarts-per-hour", type=int, default=4)
    parser.add_argument("--once", action="store_true", help="run one check and exit")
    parser.add_argument("--restore-known-good", action="store_true",
                        help="operator action: put the last known-good config back (the current file is kept) and exit")
    args = parser.parse_args(argv)
    if args.restore_known_good:
        record = restore_known_good(Path(args.config), Path(args.state_dir))
        print(json.dumps(record), flush=True)
        return 0 if record["event"] == "restored_by_operator" else 1
    if not args.health_url or not args.restart_cmd:
        parser.error("--health-url and --restart-cmd are required unless --restore-known-good is given")
    dog = GatewayWatchdog(
        HttpHealthProbe(args.health_url),
        Path(args.config),
        Path(args.state_dir),
        restart=command_restarter(args.restart_cmd),
        failures_before_action=args.failures,
        good_after_seconds=args.good_after,
        max_restarts_per_hour=args.max_restarts_per_hour,
    )

    async def loop() -> None:
        while True:
            try:
                print(json.dumps(await dog.check()), flush=True)
            except Exception as exc:  # keep watching; record why this check failed
                print(json.dumps(dog._audit({"event": "check_error", "error_class": type(exc).__name__})), flush=True)
            if args.once:
                return
            await asyncio.sleep(args.interval)

    asyncio.run(loop())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
