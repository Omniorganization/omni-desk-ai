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


class PermissionDecision:
    def __init__(self, allowed: bool, mode: str = "allow", reason: str = ""):
        self.allowed = allowed
        self.mode = mode
        self.reason = reason


class PermissionManager:
    def __init__(self, cfg: PermissionConfig, approval_store=None):
        self.cfg = cfg
        self.approval_store = approval_store
        self.session_allows: set[str] = set()
        self.metrics: Any = None
        self.audit_log = cfg.audit_log.expanduser()
        self.audit_log.parent.mkdir(parents=True, exist_ok=True)

    def session_key(self, proposal_dict: dict[str, Any]) -> str:
        source = str(proposal_dict.get("source", "unknown"))
        actor = str(proposal_dict.get("actor", "unknown"))
        risk = str(proposal_dict.get("risk", "medium"))
        scope_hash = str(proposal_dict.get("scope_hash") or "")
        tool = str(proposal_dict.get("tool", ""))
        action = str(proposal_dict.get("action", ""))
        fallback_scope = f"{tool}.{action}"
        return f"{source}|{actor}|{risk}|{scope_hash or fallback_scope}"

    def verify(self, proposal: Any) -> PermissionDecision:
        proposal_dict = self._proposal_dict(proposal)
        tool = str(proposal_dict.get("tool", ""))
        risk = str(proposal_dict.get("risk", "medium"))
        source = str(proposal_dict.get("source", "unknown"))
        session_key = self.session_key(proposal_dict)

        if risk in set(getattr(self.cfg, "require_dual_approval_for_risks", [])):
            proposal_dict["requires_dual_approval"] = True
        self._audit("proposal", proposal_dict)
        self._metric("omnidesk_approval_proposals_total", tool=tool, risk=risk, source=source)

        if risk == "low" and source in getattr(self.cfg, "allow_low_risk_from", []):
            self._audit("allowed_low_risk", proposal_dict)
            self._metric("omnidesk_approval_decisions_total", tool=tool, risk=risk, decision="allow", mode="low_risk")
            return PermissionDecision(True, "allow", "low risk allowed")

        if session_key in self.session_allows:
            self._audit("allowed_session", {"session_key": session_key, **proposal_dict})
            self._metric("omnidesk_approval_decisions_total", tool=tool, risk=risk, decision="allow", mode="session")
            return PermissionDecision(True, "allow", "session allow")

        mode = getattr(self.cfg, "approval_mode", "interactive_cli")
        default_mode = getattr(self.cfg, "default_mode", "ask")

        if mode == "auto_policy":
            if risk in {"low", "medium"} and tool not in getattr(self.cfg, "always_ask_tools", []):
                self._audit("allowed_auto_policy", proposal_dict)
                self._metric("omnidesk_approval_decisions_total", tool=tool, risk=risk, decision="allow", mode="auto_policy")
                return PermissionDecision(True, "allow", "auto_policy")
            return self._remote_or_deny(proposal_dict)

        if mode == "remote_approval":
            return self._remote_or_deny(proposal_dict)

        if default_mode == "allow":
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
        """Allow the exact approved proposal scope during a resume call.

        Remote approval is normally implemented by raising ApprovalRequired. After
        a human approves that proposal, resume must execute the originally blocked
        step once instead of creating a fresh approval for the same scope.
        """
        proposal_dict = self._proposal_dict(proposal)
        session_key = self.session_key(proposal_dict)
        self.session_allows.add(session_key)
        self._audit("allowed_after_remote_approval", {"session_key": session_key, **proposal_dict})
        self._metric("omnidesk_approval_resume_grants_total", tool=str(proposal_dict.get("tool", "")), risk=str(proposal_dict.get("risk", "medium")))
        return session_key

    def _remote_or_deny(self, proposal_dict: dict[str, Any]) -> PermissionDecision:
        if self.approval_store is None:
            self._audit("denied_no_approval_store", proposal_dict)
            raise PermissionError("remote_approval requires ApprovalStore")
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

    def _last_audit_hash(self) -> str:
        try:
            if not self.audit_log.exists() or self.audit_log.stat().st_size == 0:
                return "0" * 64
            with self.audit_log.open("rb") as f:
                f.seek(0, 2)
                pos = f.tell() - 1
                while pos > 0:
                    f.seek(pos)
                    if f.read(1) == b"\n":
                        break
                    pos -= 1
                if pos <= 0:
                    f.seek(0)
                line = f.readline().decode("utf-8", errors="replace").strip()
            parsed = json.loads(line) if line else {}
            value = str(parsed.get("hash") or "")
            return value if len(value) == 64 else "0" * 64
        except Exception:
            return "0" * 64

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
