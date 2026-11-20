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

## Stopping the Agent

- `POST /agent/runs/{run_id}/cancel` needs an operator or owner and an idempotency key. An operator can cancel only the runs started with their own identity, and gets the same 404 for someone else's run as for a missing one; an owner can cancel any run. The orchestrator records `agent_run_cancelled`; if that entry cannot be written after the run was cancelled, the response still reports the cancel and names the failure in `audit_error`.
- `POST /admin/emergency-stop/engage` needs an operator or owner: anyone who can start a run can stop them all. Engaging never waits on the audit log; the response says whether `agent_emergency_stop` was recorded.
- `POST /admin/emergency-stop/release` needs an owner and is refused through a break-glass elevation. The release is recorded as `agent_emergency_stop` before the stop is lifted; if the audit log cannot take the entry, the stop stays engaged and the route answers 503.
- `GET /admin/emergency-stop` (viewer) shows the state. All four routes answer 501 until the runtime provides run cancellation and the emergency stop.

## Reporting

Do not include secrets, credentials, private keys, tokens, customer data, or production evidence payloads in public issues. Use a private security channel controlled by the repository owner.
