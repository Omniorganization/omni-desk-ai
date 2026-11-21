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

## Approval Record Visibility

- `GET /approvals` lists only approvals whose requester belongs to the caller's organization, as the gateway resolves it, never as the client states it.
- Owners, who decide, and the requester see the full proposal. Every other caller, viewers included, sees what is asked: tool, action, risk, run, plan, step, scope hash and requester. Argument content is withheld, and so are its digests and lengths: an unsalted digest of short typed text, such as a PIN or a one-time code, can be brute-forced. The scope hash stays, so approvals can still be correlated. It is a weaker oracle of the same kind, because it covers the content together with withheld fields such as the expected result, so treat approval scope hashes as sensitive. A break-glass session that raises its holder to owner shows them the full proposal they decide. A requester whose organization cannot be resolved shows the approval to nobody.

## Reporting

Do not include secrets, credentials, private keys, tokens, customer data, or production evidence payloads in public issues. Use a private security channel controlled by the repository owner.
