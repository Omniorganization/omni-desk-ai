from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import asdict, is_dataclass
from typing import Any

from omnidesk_agent.config import PermissionConfig
from omnidesk_agent.security.approval_required import ApprovalRequired
from omnidesk_agent.security.audit_chain import DEFAULT_KEY_ENV, AuditChain, AuditIntegrityError
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
        # Keyed hash chain; the key comes from the secret store (see audit_chain).
        self.audit_chain = AuditChain(self.audit_log, key_env=str(getattr(cfg, "audit_checkpoint_hmac_key_env", "") or DEFAULT_KEY_ENV))
        # Set when an audit log fails verification. Every later decision is
        # refused until an operator seals the log (audit_chain rotate).
        self.audit_integrity_failure: str | None = None

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

    def record_access(self, proposal: Any) -> None:
        """Audit an action that runs without an approval gate (for example files.list).

        Raises AuditIntegrityError when the audit log cannot be written; the
        caller must then not perform the action.
        """
        proposal_dict = self._proposal_dict(proposal)
        self._audit("access_without_approval", proposal_dict)
        self._metric("omnidesk_audited_access_total", tool=str(proposal_dict.get("tool", "")), action=str(proposal_dict.get("action", "")))

    def record_policy_block(self, proposal: Any, *, rule: str, reason: str) -> None:
        """Audit a request that a policy check refused before verify() ran.

        ``rule`` names the check (path_escape, git_metadata_write,
        capability_disabled, command_not_allowlisted, ...). Call it, then refuse
        the request as before.
        """
        proposal_dict = self._proposal_dict(proposal)
        self._audit("policy_blocked", {**proposal_dict, "block_rule": str(rule), "block_reason": str(reason)})
        self._metric("omnidesk_policy_blocks_total", tool=str(proposal_dict.get("tool", "")), rule=str(rule))

    def mark_audit_integrity_failure(self, message: str) -> None:
        """Fail closed after an audit log failed verification (see AuditIntegrityMonitor)."""
        if self.audit_integrity_failure is None:
            self.audit_integrity_failure = str(message)

    def _audit(self, event: str, payload: dict[str, Any]) -> None:
        if self.audit_integrity_failure is not None:
            raise AuditIntegrityError(f"audit log integrity failure; refusing until it is rotated: {self.audit_integrity_failure}")
        try:
            self.audit_chain.append(event, self._redact(payload))
        except AuditIntegrityError:
            self._metric("omnidesk_audit_write_failures_total")
            raise

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
