from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
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
        self.audit_log = cfg.audit_log.expanduser()
        self.audit_log.parent.mkdir(parents=True, exist_ok=True)

    def verify(self, proposal: Any) -> PermissionDecision:
        proposal_dict = self._proposal_dict(proposal)
        tool = str(proposal_dict.get("tool", ""))
        action = str(proposal_dict.get("action", ""))
        risk = str(proposal_dict.get("risk", "medium"))
        source = str(proposal_dict.get("source", "unknown"))
        key = f"{tool}.{action}"

        self._audit("proposal", proposal_dict)

        if risk == "low" and source in getattr(self.cfg, "allow_low_risk_from", []):
            self._audit("allowed_low_risk", proposal_dict)
            return PermissionDecision(True, "allow", "low risk allowed")

        if key in self.session_allows:
            self._audit("allowed_session", proposal_dict)
            return PermissionDecision(True, "allow", "session allow")

        mode = getattr(self.cfg, "approval_mode", "interactive_cli")
        default_mode = getattr(self.cfg, "default_mode", "ask")

        if mode == "auto_policy":
            if risk in {"low", "medium"} and tool not in getattr(self.cfg, "always_ask_tools", []):
                self._audit("allowed_auto_policy", proposal_dict)
                return PermissionDecision(True, "allow", "auto_policy")
            return self._remote_or_deny(proposal_dict)

        if mode == "remote_approval":
            return self._remote_or_deny(proposal_dict)

        if default_mode == "allow":
            self._audit("allowed_default", proposal_dict)
            return PermissionDecision(True, "allow", "default allow")
        if default_mode == "dry_run":
            self._audit("dry_run", proposal_dict)
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
            self.session_allows.add(key)
            self._audit("allowed_session_new", proposal_dict)
            return PermissionDecision(True, "allow", "session")
        if ans in {"d", "dry-run"}:
            self._audit("dry_run_interactive", proposal_dict)
            return PermissionDecision(False, "dry_run", "interactive dry_run")
        self._audit("denied_interactive", proposal_dict)
        raise PermissionError("Denied by user")

    def _remote_or_deny(self, proposal_dict: dict[str, Any]) -> PermissionDecision:
        if self.approval_store is None:
            self._audit("denied_no_approval_store", proposal_dict)
            raise PermissionError("remote_approval requires ApprovalStore")
        approval_id = self.approval_store.create(proposal_dict)
        self._audit("approval_required", {"approval_id": approval_id, **proposal_dict})
        raise ApprovalRequired(approval_id=approval_id, proposal=proposal_dict)

    def _audit(self, event: str, payload: dict[str, Any]) -> None:
        clean = self._redact(payload)
        line = json.dumps({"ts": time.time(), "event": event, "payload": clean}, ensure_ascii=False)
        with self.audit_log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    @staticmethod
    def _proposal_dict(proposal: Any) -> dict[str, Any]:
        if isinstance(proposal, dict):
            return dict(proposal)
        if is_dataclass(proposal):
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
