from __future__ import annotations

import hashlib
import hmac
import json
import re
import sys
import threading
import time
from dataclasses import asdict, is_dataclass
from typing import Any

from omnidesk_agent.config import PermissionConfig
from omnidesk_agent.security.approval_required import ApprovalRequired
from omnidesk_agent.security.audit_chain import DEFAULT_KEY_ENV, AuditChain, AuditIntegrityError, dev_key_file_for, load_audit_key
from omnidesk_agent.security.dual_approval import stamp_requester
from omnidesk_agent.security.provenance import proposal_taint, requires_fresh_approval


# Upper bound for one agent-loop audit payload (record_event).
AGENT_EVENT_MAX_BYTES = 16_384

# Risks that may only run after two distinct owners approved the exact
# proposal through the remote approval store, whatever the configuration says.
# Configuration can add risks (require_dual_approval_for_risks), never remove these.
ALWAYS_DUAL_APPROVAL_RISKS = frozenset({"critical"})


class PermissionDecision:
    def __init__(self, allowed: bool, mode: str = "allow", reason: str = ""):
        self.allowed = allowed
        self.mode = mode
        self.reason = reason


# Sub-key label for keyed content digests and scope hashes. It differs from the
# chain's and the checkpoints' keys, so no digest can stand in for a chain MAC.
SCOPE_HASH_KEY_LABEL = b"omnidesk/scope-hash/v1"


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
        self._audit_key_env = str(getattr(cfg, "audit_checkpoint_hmac_key_env", "") or DEFAULT_KEY_ENV)
        self.audit_chain = AuditChain(self.audit_log, key_env=self._audit_key_env)
        # Derived on first use; the raw audit key is never kept here.
        self._digest_key: bytes | None = None
        self._digest_lock = threading.Lock()
        # Set when an audit log fails verification. Every later decision is
        # refused until an operator seals the log (audit_chain rotate).
        self.audit_integrity_failure: str | None = None

    def keyed_digest(self, data: bytes) -> str:
        """HMAC-SHA256 hex digest of ``data``, for content digests and scope hashes.

        Unkeyed SHA-256 of approved content lets anyone who can read the audit
        log or the approval records test guesses at low-entropy content, such as
        a password the agent types. The key is derived from the audit key in the
        secret store under SCOPE_HASH_KEY_LABEL, so it is the same across
        restarts and replicas, and only this object holds it. Raises
        AuditIntegrityError when the audit key is unavailable (production
        without the secret): nothing could be audited then either.
        """
        key = self._digest_key
        if key is None:
            with self._digest_lock:
                if self._digest_key is None:
                    raw = load_audit_key(self._audit_key_env, dev_key_file=dev_key_file_for(self.audit_log))
                    self._digest_key = hmac.new(raw, SCOPE_HASH_KEY_LABEL, hashlib.sha256).digest()
                key = self._digest_key
        return hmac.new(key, bytes(data), hashlib.sha256).hexdigest()

    def session_key(self, proposal_dict: dict[str, Any]) -> str:
        source = str(proposal_dict.get("source", "unknown"))
        actor = str(proposal_dict.get("actor", "unknown"))
        risk = str(proposal_dict.get("risk", "medium"))
        scope_hash = str(proposal_dict.get("scope_hash") or "")
        tool = str(proposal_dict.get("tool", ""))
        action = str(proposal_dict.get("action", ""))
        fallback_scope = f"{tool}.{action}"
        key = f"{source}|{actor}|{risk}|{scope_hash or fallback_scope}"
        taint = proposal_taint(proposal_dict)
        if taint is not None:
            # A grant covers the exact context the approver saw, untrusted inputs included.
            key += f"|taint:{taint['digest']}"
        return key

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
        # A model-proposed high/critical action taken while untrusted content was in
        # its context needs a fresh human approval of this exact proposal (provenance.py).
        fresh = requires_fresh_approval(proposal_dict)
        if fresh:
            proposal_dict["requires_fresh_approval"] = True
        self._audit("proposal", proposal_dict)
        self._metric("omnidesk_approval_proposals_total", tool=tool, risk=risk, source=source)
        if fresh:
            self._audit("fresh_approval_required_untrusted", proposal_dict)
            self._metric("omnidesk_fresh_approval_required_total", tool=tool, risk=risk)

        if not dual and risk == "low" and source in getattr(self.cfg, "allow_low_risk_from", []):
            self._audit("allowed_low_risk", proposal_dict)
            self._metric("omnidesk_approval_decisions_total", tool=tool, risk=risk, decision="allow", mode="low_risk")
            return PermissionDecision(True, "allow", "low risk allowed")

        if self._consume_resume_grant(session_key):
            self._audit("allowed_resume_grant", {"session_key": session_key, **proposal_dict})
            self._metric("omnidesk_approval_decisions_total", tool=tool, risk=risk, decision="allow", mode="resume_grant")
            return PermissionDecision(True, "allow", "remote approval resume grant")

        if not dual and not fresh and session_key in self.session_allows:
            self._audit("allowed_session", {"session_key": session_key, **proposal_dict})
            self._metric("omnidesk_approval_decisions_total", tool=tool, risk=risk, decision="allow", mode="session")
            return PermissionDecision(True, "allow", "session allow")

        mode = getattr(self.cfg, "approval_mode", "interactive_cli")
        default_mode = getattr(self.cfg, "default_mode", "ask")

        if mode == "auto_policy":
            if not dual and not fresh and risk in {"low", "medium"} and tool not in getattr(self.cfg, "always_ask_tools", []):
                self._audit("allowed_auto_policy", proposal_dict)
                self._metric("omnidesk_approval_decisions_total", tool=tool, risk=risk, decision="allow", mode="auto_policy")
                return PermissionDecision(True, "allow", "auto_policy")
            return self._remote_or_deny(proposal_dict)

        if mode == "remote_approval":
            return self._remote_or_deny(proposal_dict)

        if default_mode == "allow" and dual:
            # default allow is a development convenience; it never stands
            # in for the two remote owner approvals a dual-approval risk needs.
            self._audit("dual_approval_required", proposal_dict)
            return self._remote_or_deny(proposal_dict)
        if default_mode == "allow" and not fresh:
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
        if fresh and ans in {"y", "yes", "s", "session"}:
            # Untrusted-derived action: this answer approves this one call only.
            self._audit("allowed_interactive_fresh", proposal_dict)
            return PermissionDecision(True, "allow", "interactive fresh approval")
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

        If the audit log cannot take the entry this raises, and the caller must
        then not perform the action. It raises AuditIntegrityError with the
        keyed audit chain (security #230); before that, the file write's OSError.
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

    def record_event(self, event: str, payload: dict[str, Any]) -> None:
        """Audit an agent-loop lifecycle event (agent_turn_planned, agent_step_finished, ...).

        Only ``agent_``-prefixed names are accepted, so loop events can never pose
        as approval decisions, policy blocks or break-glass transitions. The
        payload must be JSON-serialisable and at most AGENT_EVENT_MAX_BYTES; it
        passes the same redaction as proposals and should hold ids, counts,
        digests and statuses, never tool output text. If the entry cannot be
        written this raises (AuditIntegrityError with the keyed audit chain of
        security #230, OSError before): stop the run then.
        """
        name = str(event or "")
        if not re.fullmatch(r"agent_[a-z0-9_]{1,60}", name):
            raise ValueError("agent loop audit events must be named agent_<lower_snake_case>")
        if not isinstance(payload, dict):
            raise TypeError("agent loop audit payload must be a dict")
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("agent loop audit payload must be JSON-serialisable") from exc
        if len(encoded.encode("utf-8")) > AGENT_EVENT_MAX_BYTES:
            raise ValueError(f"agent loop audit payload exceeds {AGENT_EVENT_MAX_BYTES} bytes")
        # Log exactly what was checked: tuples become lists, so redaction reaches
        # every nested key, and the size cap applies to what is written.
        self._audit(name, json.loads(encoded))

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
