# Security Policy

## Supported Status

Current status: `1.12.7+root-monorepo-production-ga-candidate`.

This is a source-gated production GA candidate. It must not be represented as customer-distribution Production GA until external evidence gates pass against real systems.

## Required Controls

- Do not weaken approval, dual approval, sandbox, RBAC, CSP, webhook signatures, device signatures, audit logs, or external GA evidence gates.
- L2 and higher write/action capabilities require approval gating.
- L3 and L4 actions require human approval, auditability, timeout/revocation behavior, and rollback or containment evidence.
- Signed device requests are required for sensitive Desktop and Mobile runtime actions.
- Release artifacts must include portable checksums and release manifests.
- Customer-distribution releases require signed artifacts, SBOM/provenance, image digests, and real external evidence.

## Approval Enforcement

What the code enforces today (`omnidesk_agent/security/permissions.py`, `approval_store.py`, `dual_approval.py`):

- Every side-effecting tool calls `PermissionManager.verify()` with a risk level fixed by the tool, not by the plan.
- `critical` actions, and any other risk listed in `permissions.require_dual_approval_for_risks`, run only after two distinct owners, neither of them the requester, approve the exact proposal through the remote approval store. No setting takes `critical` out of this rule. `default_mode=allow`, a console `y`, an interactive session allow, `auto_policy` and the local low-risk shortcut never authorize these actions. Without an approval store they are denied. Stricter settings still apply to them: `default_mode=deny` or `dry_run` and `no_tty_mode`.
- A remote approval authorizes one execution. The resume grant is used up by the step it approved and expires after `approval_ttl_seconds`.
- In production, the configuration validator refuses `permissions.default_mode=allow` and requires `critical` in `require_dual_approval_for_risks`.
- Known limits:
  - Approver identities are static per-role bearer tokens. The owner token and the admin token count as two distinct owners; no per-person identity exists yet.
  - AppSync task approvals (`/app/approvals`) do not apply dual approval.

## Audit Log Integrity

What the code enforces today (`omnidesk_agent/security/audit_chain.py`, `audit_worm.py`):

- The security audit log (`permissions.audit_log`: approval decisions, break-glass transitions, policy blocks, agent-loop `agent_*` events) and the admin authentication log (`admin_auth_audit.jsonl`, next to the security audit log, outside `workspace.root`) are HMAC-SHA256 hash chains.
  - Every entry carries a sequence number and the previous entry's MAC.
  - The MAC key is derived from `OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY` (the `permissions.audit_checkpoint_hmac_key_env` setting), which production must load from the secret store. The production validator refuses to start without it, and nothing in production falls back to a local key.
  - Outside production, when the variable is unset, a random per-log key is created once next to the log (`<log>.key`, mode 0600). It protects against accidents only.
- Writers fail closed and never restart the chain. When the last line is partial or not JSON, an unchained line follows the chain, or the last entry fails its MAC, the permission gate refuses the action, break-glass refuses the transition, and admin authentication refuses the request. A break-glass session whose audit entry could not be written is revoked.
- A log written before the chain existed is upgraded on the next write. The first keyed entry commits to the SHA-256 of all earlier bytes.
- Verification:
  - `python -m omnidesk_agent.security.audit_chain verify <log>...` verifies the whole chain. It exits 1 on any edited, inserted, deleted or reordered entry, a wrong key, or a damaged tail, and exits 2 when no key is available.
  - With `--checkpoint-dir`, it also checks the newest signed anchor and writes a new one. Truncating the tail is then caught as well, because the anchored entry must still be present. Mirror anchor files to storage the gateway cannot delete.
- The gateway re-verifies both logs in the background. Every `OMNIDESK_AUDIT_VERIFY_INTERVAL_SECONDS` (default 300) it checks what was appended since the last check, and every `OMNIDESK_AUDIT_FULL_VERIFY_INTERVAL_SECONDS` (default 3600) it re-verifies everything. On a failure every permission decision is refused, `/ready` reports not ready, and `omnidesk_audit_chain_verify_failures_total` increases.
- Recovery: keep the damaged log as evidence, then run `python -m omnidesk_agent.security.audit_chain rotate <log> --reason "..."`. It seals the log under a new name and starts a chain whose first entry records the sealed file's SHA-256 and verification result. To rotate the key, use `--previous-key-env`.
- CI (`security-attack-surface`) runs `scripts/check_audit_chain.py`. The script writes a log through the real writers on the CI runner, verifies it, and checks that the verifier rejects each kind of damage. That is evidence about the verifier, not about any production log.
- Known limits:
  - Anyone holding the key can rebuild the chain.
  - Without off-host anchor copies, deleting the newest entries is only caught by anchors that were not deleted with them.
  - A chain detects edits; it does not prevent them. Logs written before this change, at `workspace.root/admin_auth_audit.jsonl`, are left where they are.

## Prompt-Injection Defences

What the code provides today (`omnidesk_agent/security/provenance.py`, `prompt_injection.py`):

- Text that reaches a model is tagged with its provenance:
  - `system`: OmniDesk's own instructions;
  - `user`: the authenticated requester's task;
  - `untrusted`: anything else, including tool output, page, file or mail contents, third-party messages and retrieved memory.
- `PromptEnvelope` keeps instructions and data apart. Only OmniDesk's instructions go into the system prompt. Each data item is fenced with a random per-envelope boundary that is removed from the data itself, so the data cannot close its fence. The planner's older `build_untrusted_task_payload` strips its own delimiter tags from the message for the same reason. Keyword detection (`detect_prompt_injection`) only adds informational flags; it does not decide trust.
- `TaintTracker` records per run whether untrusted text reached the model. Its snapshot travels with each model-proposed tool call as `ActionProposal.provenance`. When it is tainted and the risk is `high` or `critical`, `PermissionManager.verify()` requires a fresh approval of that exact proposal:
  - `default_mode=allow`, `auto_policy`, interactive session allows and earlier grants do not apply;
  - a console answer approves one call only;
  - in remote mode the approval record carries the provenance and `requires_fresh_approval`;
  - grants are bound to the provenance digest;
  - `critical` still needs two owners;
  - malformed provenance counts as tainted.
- Known limits:
  - The agent loop must route model input through these helpers and set `ToolContext.provenance`. Until that wiring lands (agent-core), the rule applies only to proposals that carry provenance.
  - Medium-risk actions derived from untrusted text follow the normal policy.

## Reporting

Do not include secrets, credentials, private keys, tokens, customer data, or production evidence payloads in public issues. Use a private security channel controlled by the repository owner.
