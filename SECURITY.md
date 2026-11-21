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
- In production, the configuration validator requires `permissions.approval_mode=remote_approval`, refuses `permissions.default_mode=allow`, and requires `critical` in `require_dual_approval_for_risks`. Remote approvals are the ones recorded with an authenticated approver.
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

## Admin Sessions

Web Admin and other admin clients should not keep the long-lived role token. They exchange it for a short-lived, revocable session token (`security/admin_sessions.py`, `server_routes/admin_session_routes.py`).

- **Login.** `POST /admin/session/login` with a role token (viewer and up) returns an `omds_...` token once.
  - The session has that token's actor and role, and the organization the gateway assigns to the actor. It lasts 8 hours by default and at most 12.
  - Only a real credential opens a session: a role token or the legacy gateway secret. A session token cannot open another session, local-development access (no token, loopback only) cannot become a session usable from anywhere, and a break-glass elevation cannot open one.
  - The response is `Cache-Control: no-store`. The route takes no idempotency key, so the token is never cached.
- **Use.** Send the session token like a role token (`Authorization: Bearer omds_...`). Expired, revoked or tampered sessions are refused.
- **Storage.**
  - The store keeps an HMAC of the token, never the token itself.
  - Each row is sealed over its id, actor, role, organization and expiry. Both keys derive from the audit key in the secret store, with their own labels. A row inserted or edited without that key does not authenticate.
  - SQLite sits next to the audit log, outside the workspace, and the name is also runtime state. PostgreSQL uses one shared table, so every gateway instance sees the same sessions.
- **Revoke.** `POST /admin/session/revoke`:
  - `{}` revokes the current session (logout);
  - `{"session_id": ...}` revokes one of the caller's sessions; an owner can revoke anyone's;
  - `{"all": true}` revokes all of the caller's sessions; `{"all": true, "actor": ...}` is owner-only.
  - Revocation takes effect on the next request. It works even while the admin audit log cannot take entries: the revoker is authenticated with best-effort auditing, because revoking only takes access away. Every other admin request still fails closed.
- **Limits.** At most 10 logins a minute and 20 live sessions per actor (429 with `Retry-After`). They are counted in the shared store.
- **Audit.** `admin_session.login` and `admin_session.revoke` go to the chained admin audit log with the session id, never the token. Every request made with a session records its session id. A login that cannot be audited is revoked again (503). A revoke is never undone because its audit entry failed.
- **Known limit.** Revocation is a column, not sealed history. Someone who can write the table and kept a copy of a row from before its revocation could restore it until its original expiry.

## Reading the Audit Logs

`GET /admin/audit/events` (`server_routes/audit_routes.py`) is the read API for Web Admin's audit view.

- **Who.** Auditors and owners only. `auditor` is a role of its own (`OMNIDESK_AUDITOR_TOKEN`, `OMNIDESK_AUDITOR_ACTOR`). It can do what a viewer can, plus read the audit logs, and nothing else. A route that requires `auditor` refuses operators, and break-glass never grants it.
- **What.** `log=security` is the permission gate's log; `log=admin` is admin authentication and sessions. `after_seq` and `limit` (at most 500) page through the chain in order.
- **Scope.** An entry belongs to the organization of its actor. Entries with no actor are deployment-level and shown only to callers in `org_default`.
- **Redaction.**
  - Argument values are withheld; their digests and lengths are kept.
  - Content fields (message text, tool output, model replies) are withheld.
  - Secret-named keys are masked, and credentials inside the remaining strings are masked.
- **Integrity.** `verification` reports the background chain check for that log: `ok`, `failed` or `not_checked`, when it last ran, the last sequence it verified, and its error.

## Reporting

Do not include secrets, credentials, private keys, tokens, customer data, or production evidence payloads in public issues. Use a private security channel controlled by the repository owner.
