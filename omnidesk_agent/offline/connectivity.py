from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


CONNECTIVITY_STATES = (
    "offline",
    "local_only",
    "online_detected",
    "reconnecting",
    "syncing",
    "update_checking",
    "update_available",
    "downloading",
    "downloaded",
    "verifying",
    "verified",
    "staging",
    "staged",
    "health_check",
    "healthy",
    "activate",
    "failed",
    "rollback",
)


@dataclass(frozen=True)
class ProbeResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class ConnectivityReport:
    state: str
    online: bool
    probes: list[ProbeResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "online": self.online,
            "probes": [probe.__dict__ for probe in self.probes],
        }


class SyncStore(Protocol):
    def pending_local_outbox(self, *, actor: str | None = None, limit: int = 100) -> list[dict[str, Any]]: ...
    def mark_local_operation_synced(self, operation_id: str, *, remote_seq: int | None = None) -> dict[str, Any]: ...
    def mark_local_operation_failed(self, operation_id: str, *, error: str, retry_delay_seconds: int = 60) -> dict[str, Any]: ...
    def set_network_state(self, state: str, *, reason: str | None = None) -> dict[str, Any]: ...
    def sync_state(self, *, actor: str | None = None) -> dict[str, Any]: ...


class ConnectivityManager:
    def __init__(
        self,
        *,
        dns_host: str = "github.com",
        manifest_url: str | None = None,
        backend_health_url: str | None = None,
        time_url: str | None = None,
        timeout_seconds: float = 2.0,
        probes: list[Callable[[], ProbeResult]] | None = None,
    ):
        self.dns_host = dns_host
        self.manifest_url = manifest_url
        self.backend_health_url = backend_health_url
        self.time_url = time_url
        self.timeout_seconds = timeout_seconds
        self._custom_probes = probes or []
        self.state = "local_only"

    def evaluate(self) -> ConnectivityReport:
        probes = self._custom_probes[:] if self._custom_probes else self._default_probes()
        results: list[ProbeResult] = []
        for probe in probes:
            try:
                results.append(probe())
            except Exception as exc:
                results.append(ProbeResult(getattr(probe, "__name__", "probe"), False, str(exc)[:200]))
        online = bool(results) and all(result.ok for result in results)
        self.state = "online_detected" if online else "local_only"
        return ConnectivityReport(state=self.state, online=online, probes=results)

    def transition(self, new_state: str) -> str:
        if new_state not in CONNECTIVITY_STATES:
            raise ValueError(f"unknown connectivity state: {new_state}")
        self.state = new_state
        return self.state

    def _default_probes(self) -> list[Callable[[], ProbeResult]]:
        probes: list[Callable[[], ProbeResult]] = [self._dns_probe]
        if self.manifest_url:
            if self.manifest_url.startswith(("http://", "https://")):
                probes.append(lambda: self._url_probe("manifest", self.manifest_url or ""))
            else:
                probes.append(self._manifest_file_probe)
        if self.backend_health_url:
            probes.append(lambda: self._url_probe("backend_health", self.backend_health_url or ""))
        if self.time_url:
            probes.append(lambda: self._url_probe("time_source", self.time_url or ""))
        return probes

    def _dns_probe(self) -> ProbeResult:
        try:
            socket.getaddrinfo(self.dns_host, 443, proto=socket.IPPROTO_TCP)
            return ProbeResult("dns", True, self.dns_host)
        except OSError as exc:
            return ProbeResult("dns", False, str(exc))

    def _url_probe(self, name: str, url: str) -> ProbeResult:
        try:
            request = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # nosec B310 - operator-configured health endpoint
                return ProbeResult(name, response.status < 500, f"status={response.status}")
        except urllib.error.HTTPError as exc:
            return ProbeResult(name, exc.code < 500, f"status={exc.code}")
        except OSError as exc:
            return ProbeResult(name, False, str(exc)[:200])

    def _manifest_file_probe(self) -> ProbeResult:
        from pathlib import Path

        path = Path(str(self.manifest_url or "").removeprefix("file://")).expanduser()
        return ProbeResult("manifest", path.exists(), str(path))


class ReconnectSyncWorker:
    def __init__(
        self,
        *,
        connectivity: ConnectivityManager,
        store: SyncStore,
        actor: str = "system",
        upload: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
        update_check: Callable[[], dict[str, Any]] | None = None,
    ):
        self.connectivity = connectivity
        self.store = store
        self.actor = actor
        self.upload = upload
        self.update_check = update_check

    def run_once(self) -> dict[str, Any]:
        report = self.connectivity.evaluate()
        if not report.online:
            self.store.set_network_state("local_only", reason="connectivity probes failed")
            return {"ok": True, "state": "local_only", "connectivity": report.to_dict(), "synced": 0, "update": None}

        self.connectivity.transition("reconnecting")
        self.store.set_network_state("reconnecting", reason="connectivity probes passed")
        self.connectivity.transition("syncing")
        self.store.set_network_state("syncing")
        pending = self.store.pending_local_outbox(actor=self.actor, limit=100)
        synced = 0
        upload_result: dict[str, Any] = {"ok": True}
        if pending and self.upload is None:
            self.connectivity.transition("failed")
            self.store.set_network_state("failed", reason="outbox upload target is not configured")
            return {"ok": False, "state": "failed", "connectivity": report.to_dict(), "error": "outbox upload target is not configured", "synced": 0, "update": None}
        if pending and self.upload is not None:
            try:
                upload_result = self.upload(pending)
            except Exception as exc:
                for item in pending:
                    self.store.mark_local_operation_failed(str(item.get("operation_id")), error=str(exc), retry_delay_seconds=60)
                self.connectivity.transition("failed")
                self.store.set_network_state("failed", reason="outbox upload failed")
                return {"ok": False, "state": "failed", "connectivity": report.to_dict(), "error": str(exc), "synced": 0, "update": None}
        for item in pending:
            self.store.mark_local_operation_synced(str(item.get("operation_id")), remote_seq=upload_result.get("remote_seq"))
            synced += 1
        state = self.store.sync_state(actor=self.actor)
        if state.get("conflicts", {}).get("open", 0):
            self.connectivity.transition("failed")
            self.store.set_network_state("failed", reason="sync conflicts require manual review")
            return {"ok": False, "state": "failed", "connectivity": report.to_dict(), "synced": synced, "update": None, "sync_state": state}

        update_result = None
        if self.update_check is not None:
            self.connectivity.transition("update_checking")
            self.store.set_network_state("update_checking")
            update_result = self.update_check()
        self.connectivity.transition("healthy")
        self.store.set_network_state("healthy", reason=f"synced={synced}")
        return {
            "ok": True,
            "state": "healthy",
            "connectivity": report.to_dict(),
            "synced": synced,
            "upload": upload_result,
            "update": update_result,
            "finished_at": time.time(),
        }
