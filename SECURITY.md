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

## Data-Subject Requests

What the code provides today (`privacy/data_subject.py`, `server_routes/privacy_routes.py`):

- **Export.** `GET /admin/privacy/subjects/{actor}/export` (owner) returns what every participating store holds about the actor, plus the security and admin-auth audit entries that concern them.
- **Erase.** `POST /admin/privacy/subjects/{actor}/erase-requests` opens a `critical` approval bound to that one actor. Opening it erases nothing.
  - Two owners other than the requester must dual-approve it, and then it must be approved.
  - Only then can `POST /admin/privacy/subjects/{actor}/erase/{approval_id}` (owner) use it, once, for that actor only.
- **What results say.**
  - Every result lists the stores that could not take part (`unsupported`) and the records kept on purpose (`retained`): the tamper-evident audit logs, which are kept for their retention period and never rewritten.
  - Every export and erasure is audited with counts only, never content.
- **Participating stores.** A store takes part by implementing `export_subject(actor)` and `erase_subject(actor)`. Today only the approval records (SQLite) and their dual-approval rows do. Memory, runs, outbound messages and AppSync report `unsupported` until their owners add the hooks, so a result says `complete: false` until then.

## Reporting

Do not include secrets, credentials, private keys, tokens, customer data, or production evidence payloads in public issues. Use a private security channel controlled by the repository owner.
