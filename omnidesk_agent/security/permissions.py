from __future__ import annotations

import fnmatch
import json
import sys
import time
from pathlib import Path
from typing import Iterable

from omnidesk_agent.config import PermissionConfig
from omnidesk_agent.security.approval_required import ApprovalRequired
from omnidesk_agent.core.models import ActionProposal, ApprovalDecision


class PermissionDenied(RuntimeError):
    pass


class PermissionManager:
    """Per-action approval gate.

    This object is intentionally boring: all side-effect tools call verify() before doing
    anything. In foreground CLI mode it asks the operator. In daemon/no-tty mode it denies
    unless the policy can safely auto-allow.
    """

    def __init__(self, config: PermissionConfig):
        self.config = config
        self.audit_path: Path = config.audit_log
        self._session_allow: set[str] = set()

    def _write_audit(self, proposal: ActionProposal, decision: ApprovalDecision) -> None:
        row = {
            "ts": time.time(),
            "action_id": proposal.action_id,
            "tool": proposal.tool,
            "action": proposal.action,
            "risk": proposal.risk,
            "source": proposal.source,
            "actor": proposal.actor,
            "reason": proposal.reason,
            "args": self._redact(proposal.args),
            "decision": decision.mode,
            "allowed": decision.allowed,
            "decision_reason": decision.reason,
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _redact(self, obj):
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                lk = str(k).lower()
                if any(s in lk for s in ["token", "password", "secret", "key", "cookie"]):
                    out[k] = "<redacted>"
                else:
                    out[k] = self._redact(v)
            return out
        if isinstance(obj, list):
            return [self._redact(v) for v in obj]
        return obj

    def _shell_is_denied(self, command: str) -> str | None:
        for pattern in self.config.deny_shell_patterns:
            if fnmatch.fnmatch(command.lower(), pattern.lower()) or pattern.lower() in command.lower():
                return f"Shell command matches denied pattern: {pattern}"
        return None

    def verify(self, proposal: ActionProposal) -> ApprovalDecision:
        if proposal.tool == "shell":
            command = str(proposal.args.get("command", ""))
            deny_reason = self._shell_is_denied(command)
            if deny_reason:
                decision = ApprovalDecision(False, "deny", deny_reason)
                self._write_audit(proposal, decision)
                raise PermissionDenied(deny_reason)

        # Low-risk local reads can be auto-allowed when configured.
        if (
            proposal.risk == "low"
            and proposal.source in self.config.allow_low_risk_from
            and proposal.tool not in self.config.always_ask_tools
        ):
            decision = ApprovalDecision(True, "allow", "auto-allowed low risk local action")
            self._write_audit(proposal, decision)
            return decision

        fingerprint = f"{proposal.tool}:{proposal.action}:{proposal.risk}:{proposal.source}:{proposal.actor}"
        if fingerprint in self._session_allow:
            decision = ApprovalDecision(True, "allow", "allowed earlier in this session")
            self._write_audit(proposal, decision)
            return decision

        mode = self.config.default_mode
        if not sys.stdin.isatty():
            mode = self.config.no_tty_mode

        if mode == "allow":
            decision = ApprovalDecision(True, "allow", "policy default allow")
            self._write_audit(proposal, decision)
            return decision
        if mode == "dry_run":
            decision = ApprovalDecision(False, "dry_run", "policy dry-run")
            self._write_audit(proposal, decision)
            return decision
        if mode == "deny":
            decision = ApprovalDecision(False, "deny", "policy denied without interactive approval")
            self._write_audit(proposal, decision)
            raise PermissionDenied(decision.reason)

        # Interactive ask.
        print("\n需要权限验证：")
        print(f"  action_id: {proposal.action_id}")
        print(f"  source:    {proposal.source}")
        print(f"  actor:     {proposal.actor}")
        print(f"  tool:      {proposal.tool}.{proposal.action}")
        print(f"  risk:      {proposal.risk}")
        print(f"  reason:    {proposal.reason}")
        print(f"  args:      {json.dumps(self._redact(proposal.args), ensure_ascii=False)}")
        ans = input("批准执行？[y]一次 / [s]本会话同类 / [n]拒绝 / [d]dry-run: ").strip().lower()
        if ans == "s":
            self._session_allow.add(fingerprint)
            decision = ApprovalDecision(True, "allow", "operator approved session scope")
        elif ans == "y":
            decision = ApprovalDecision(True, "allow", "operator approved once")
        elif ans == "d":
            decision = ApprovalDecision(False, "dry_run", "operator chose dry-run")
        else:
            decision = ApprovalDecision(False, "deny", "operator denied")

        self._write_audit(proposal, decision)
        if not decision.allowed and decision.mode != "dry_run":
            raise PermissionDenied(decision.reason)
        return decision
