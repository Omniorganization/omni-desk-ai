from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from dataclasses import asdict, is_dataclass
from typing import Any

from omnidesk_agent.config import PermissionConfig
from omnidesk_agent.security.approval_required import ApprovalRequired
from omnidesk_agent.security.dual_approval import stamp_requester


# Risks that may only run after two distinct owners approved the exact
# proposal through the remote approval store, whatever the configuration says.
# Configuration can add risks (require_dual_approval_for_risks), never remove these.
ALWAYS_DUAL_APPROVAL_RISKS = frozenset({"critical"})


class PermissionDecision:
    def __init__(self, allowed: bool, mode: str = "allow", reason: str = ""):
        self.allowed = allowed
        self.mode = mode
        self.reason = reason


class PermissionManager:
    _AUDIT_TAIL_CHUNK_BYTES = 8192

    def __init__(self, cfg: PermissionConfig, approval_store=None):
        self.cfg = cfg
        self.approval_store = approval_store
        # Interactive "[s]ession" answers. Never consulted for dual-approval risks.
        self.session_allows: set[str] = set()
        # Single-use grants for a remotely approved proposal that a resume is
        # about to execute: session key -> monotonic expiry.
        self._resume_grants: dict[str, float] = {}
        self.metrics: Any = None
        self.audit_log = cfg.audit_log.expanduser()
        self.audit_log.parent.mkdir(parents=True, exist_ok=True)
        self._cached_last_hash: str | None = None
        self._cached_audit_size: int | None = None
        self._cached_audit_mtime_ns: int | None = None

    def session_key(self, proposal_dict: dict[str, Any]) -> str:
        source = str(proposal_dict.get("source", "unknown"))
        actor = str(proposal_dict.get("actor", "unknown"))
        risk = str(proposal_dict.get("risk", "medium"))
        scope_hash = str(proposal_dict.get("scope_hash") or "")
        tool = str(proposal_dict.get("tool", ""))
        action = str(proposal_dict.get("action", ""))
        fallback_scope = f"{tool}.{action}"
        return f"{source}|{actor}|{risk}|{scope_hash or fallback_scope}"

    def dual_approval_risks(self) -> frozenset[str]:
        return frozenset(getattr(self.cfg, "require_dual_approval_for_risks", None) or ()) | ALWAYS_DUAL_APPROVAL_RISKS

    def verify(self, proposal: Any) -> PermissionDecision:
        proposal_dict = self._proposal_dict(proposal)
        tool = str(proposal_dict.get("tool", ""))
        risk = str(proposal_dict.get("risk", "medium"))
        source = str(proposal_dict.get("source", "unknown"))
        session_key = self.session_key(proposal_dict)

        dual = risk in self.dual_approval_risks()
        if dual:
            proposal_dict["requires_dual_approval"] = True
        self._audit("proposal", proposal_dict)
        self._metric("omnidesk_approval_proposals_total", tool=tool, risk=risk, source=source)

        if not dual and risk == "low" and source in getattr(self.cfg, "allow_low_risk_from", []):
            self._audit("allowed_low_risk", proposal_dict)
            self._metric("omnidesk_approval_decisions_total", tool=tool, risk=risk, decision="allow", mode="low_risk")
            return PermissionDecision(True, "allow", "low risk allowed")

        if self._consume_resume_grant(session_key):
            self._audit("allowed_resume_grant", {"session_key": session_key, **proposal_dict})
            self._metric("omnidesk_approval_decisions_total", tool=tool, risk=risk, decision="allow", mode="resume_grant")
            return PermissionDecision(True, "allow", "remote approval resume grant")

        if not dual and session_key in self.session_allows:
            self._audit("allowed_session", {"session_key": session_key, **proposal_dict})
            self._metric("omnidesk_approval_decisions_total", tool=tool, risk=risk, decision="allow", mode="session")
            return PermissionDecision(True, "allow", "session allow")

        mode = getattr(self.cfg, "approval_mode", "interactive_cli")
        default_mode = getattr(self.cfg, "default_mode", "ask")

        if mode == "auto_policy":
            if not dual and risk in {"low", "medium"} and tool not in getattr(self.cfg, "always_ask_tools", []):
                self._audit("allowed_auto_policy", proposal_dict)
                self._metric("omnidesk_approval_decisions_total", tool=tool, risk=risk, decision="allow", mode="auto_policy")
                return PermissionDecision(True, "allow", "auto_policy")
            return self._remote_or_deny(proposal_dict)

        if mode == "remote_approval":
            return self._remote_or_deny(proposal_dict)

        if default_mode == "allow":
            if dual:
                # default allow is a development convenience; it never stands
                # in for the two remote owner approvals a dual-approval risk needs.
                self._audit("dual_approval_required", proposal_dict)
                return self._remote_or_deny(proposal_dict)
            self._audit("allowed_default", proposal_dict)
            self._metric("omnidesk_approval_decisions_total", tool=tool, risk=risk, decision="allow", mode="default")
            return PermissionDecision(True, "allow", "default allow")
        if default_mode == "dry_run":
            self._audit("dry_run", proposal_dict)
            self._metric("omnidesk_approval_decisions_total", tool=tool, risk=risk, decision="dry_run", mode="default")
            return PermissionDecision(False, "dry_run", "default dry_run")
        if default_mode == "deny":
            self._audit("denied_default", proposal_dict)
            raise PermissionError("Denied by default permission policy")

        if not sys.stdin.isatty():
            no_tty = getattr(self.cfg, "no_tty_mode", "deny")
            if no_tty == "dry_run":
                self._audit("dry_run_no_tty", proposal_dict)
                return PermissionDecision(False, "dry_run", "no tty dry_run")
            self._audit("denied_no_tty", proposal_dict)
            raise PermissionError("Permission required but no TTY available")

        if dual:
            # A console 'y' is one unattributed approval; dual-approval risks
            # wait for two distinct owners on the remote approval path instead.
            self._audit("dual_approval_required", proposal_dict)
            return self._remote_or_deny(proposal_dict)

        print("\nPermission request:")
        print(json.dumps(proposal_dict, ensure_ascii=False, indent=2))
        ans = input("Allow? [y]es / [n]o / [s]ession / [d]ry-run: ").strip().lower()
        if ans in {"y", "yes"}:
            self._audit("allowed_interactive", proposal_dict)
            return PermissionDecision(True, "allow", "interactive")
        if ans in {"s", "session"}:
            self.session_allows.add(session_key)
            self._audit("allowed_session_new", {"session_key": session_key, **proposal_dict})
            return PermissionDecision(True, "allow", "session")
        if ans in {"d", "dry-run"}:
            self._audit("dry_run_interactive", proposal_dict)
            self._metric("omnidesk_approval_decisions_total", tool=tool, risk=risk, decision="dry_run", mode="interactive")
            return PermissionDecision(False, "dry_run", "interactive dry_run")
        self._audit("denied_interactive", proposal_dict)
        self._metric("omnidesk_approval_decisions_total", tool=tool, risk=risk, decision="deny", mode="interactive")
        raise PermissionError("Denied by user")

    def allow_approved_proposal(self, proposal: Any) -> str:
        """Allow the exact approved proposal scope once, for the resume call.

        Remote approval is normally implemented by raising ApprovalRequired. After
        a human approves that proposal, resume must execute the originally blocked
        step once instead of creating a fresh approval for the same scope. The
        grant is used up by that execution (or expires with the approval TTL), so
        one approval never authorizes a second run of the same scope.
        """
        proposal_dict = self._proposal_dict(proposal)
        session_key = self.session_key(proposal_dict)
        ttl = max(1, int(getattr(self.cfg, "approval_ttl_seconds", 600) or 600))
        self._resume_grants[session_key] = time.monotonic() + ttl
        self._audit("allowed_after_remote_approval", {"session_key": session_key, **proposal_dict})
        self._metric("omnidesk_approval_resume_grants_total", tool=str(proposal_dict.get("tool", "")), risk=str(proposal_dict.get("risk", "medium")))
        return session_key

    def _consume_resume_grant(self, session_key: str) -> bool:
        now = time.monotonic()
        for key in [k for k, expires_at in self._resume_grants.items() if expires_at <= now]:
            self._resume_grants.pop(key, None)
        return self._resume_grants.pop(session_key, None) is not None

    def _remote_or_deny(self, proposal_dict: dict[str, Any]) -> PermissionDecision:
        if self.approval_store is None:
            self._audit("denied_no_approval_store", proposal_dict)
            raise PermissionError("remote_approval requires ApprovalStore")
        # The proposal actor comes from the authenticated message sender (ToolContext),
        # so it is the trusted requester; record it the same way POST /approvals does.
        requester = str(proposal_dict.get("actor") or "").strip()
        if requester:
            stamp_requester(proposal_dict, requester)
        elif proposal_dict.get("requires_dual_approval"):
            self._audit("denied_no_requester", proposal_dict)
            raise PermissionError("dual approval requires an authenticated requester")
        approval_id = self.approval_store.create(proposal_dict)
        self._audit("approval_required", {"approval_id": approval_id, **proposal_dict})
        self._metric("omnidesk_approval_required_total", tool=str(proposal_dict.get("tool", "")), risk=str(proposal_dict.get("risk", "medium")))
        raise ApprovalRequired(approval_id=approval_id, proposal=proposal_dict)

    def _audit(self, event: str, payload: dict[str, Any]) -> None:
        clean = self._redact(payload)
        previous_hash = self._last_audit_hash()
        entry = {
            "ts": time.time(),
            "event": event,
            "payload": clean,
            "previous_hash": previous_hash,
        }
        canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        entry["hash"] = hashlib.sha256((previous_hash + "." + canonical).encode("utf-8")).hexdigest()
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        with self.audit_log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        self._cache_last_hash(str(entry["hash"]))

    def _last_audit_hash(self) -> str:
        try:
            if not self.audit_log.exists():
                self._cache_last_hash("0" * 64, size=0, mtime_ns=0)
                return "0" * 64
            stat = self.audit_log.stat()
            if stat.st_size == 0:
                self._cache_last_hash("0" * 64, size=0, mtime_ns=stat.st_mtime_ns)
                return "0" * 64
            if (
                self._cached_last_hash
                and self._cached_audit_size == stat.st_size
                and self._cached_audit_mtime_ns == stat.st_mtime_ns
            ):
                return self._cached_last_hash
            line = self._read_last_audit_line()
            parsed = json.loads(line) if line else {}
            value = str(parsed.get("hash") or "")
            last_hash = value if len(value) == 64 else "0" * 64
            self._cache_last_hash(last_hash, size=stat.st_size, mtime_ns=stat.st_mtime_ns)
            return last_hash
        except Exception:
            return "0" * 64

    def _read_last_audit_line(self) -> str:
        with self.audit_log.open("rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            if pos <= 0:
                return ""
            buffer = b""
            while pos > 0:
                read_size = min(self._AUDIT_TAIL_CHUNK_BYTES, pos)
                pos -= read_size
                f.seek(pos)
                buffer = f.read(read_size) + buffer
                stripped = buffer.rstrip(b"\n")
                separator = stripped.rfind(b"\n")
                if separator >= 0:
                    return stripped[separator + 1 :].decode("utf-8", errors="replace").strip()
            return buffer.rstrip(b"\n").decode("utf-8", errors="replace").strip()

    def _cache_last_hash(self, value: str, *, size: int | None = None, mtime_ns: int | None = None) -> None:
        self._cached_last_hash = value if len(value) == 64 else "0" * 64
        if size is not None and mtime_ns is not None:
            self._cached_audit_size = size
            self._cached_audit_mtime_ns = mtime_ns
            return
        try:
            stat = self.audit_log.stat()
        except OSError:
            self._cached_audit_size = 0
            self._cached_audit_mtime_ns = 0
        else:
            self._cached_audit_size = stat.st_size
            self._cached_audit_mtime_ns = stat.st_mtime_ns

    def _metric(self, name: str, **labels: Any) -> None:
        metrics = getattr(self, "metrics", None)
        inc = getattr(metrics, "inc", None)
        if callable(inc):
            inc(name, **labels)

    @staticmethod
    def _proposal_dict(proposal: Any) -> dict[str, Any]:
        if isinstance(proposal, dict):
            return dict(proposal)
        if is_dataclass(proposal) and not isinstance(proposal, type):
            return asdict(proposal)
        if hasattr(proposal, "__dict__"):
            return dict(proposal.__dict__)
        return {"proposal": str(proposal)}

    @staticmethod
    def _redact(value: Any) -> Any:
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if re.search(r"(token|secret|password|api_key|authorization)", str(k), re.I):
                    out[k] = "[REDACTED]"
                else:
                    out[k] = PermissionManager._redact(v)
            return out
        if isinstance(value, list):
            return [PermissionManager._redact(v) for v in value]
        return value
