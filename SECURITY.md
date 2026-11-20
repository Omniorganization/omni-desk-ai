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
- **Participating stores.** A store takes part by implementing `export_subject(actor)` and `erase_subject(actor)`. Today only the approval records (SQLite) and their dual-approval rows do. Memory, runs, outbound messages, the job queue (every inbound task message and its result) and AppSync report `unsupported` until their owners add the hooks, so a result says `complete: false` until then.
- **Where erasure reaches, and what remains.**
  - **Gateway replicas.** Erasure runs against the gateway's stores. On PostgreSQL every gateway replica shares them, so it is complete for all replicas at once. A JSON or SQLite store belongs to one gateway.
  - **Devices' local-first stores.** Erasure does not yet reach the copies on desktop and mobile devices.
    - It emits no sync event, on purpose: an event naming the subject would copy the identifier into every replica's event log.
    - Planned (gateway-clients): the gateway keeps an erasure ledger, `HMAC(gateway key, actor)` plus the erased record ids. Sync ingress refuses writes by an erased subject or about an erased record, so an offline outbox cannot bring them back. Devices get a `subject.erased` sync event that carries only the erased record ids, and delete those records locally.
    - Until then, the erasure revokes the subject's own devices, but other members' devices may keep cached copies of the subject's messages until they are reset. Tell the requester so.
  - **Backups.** Erasure does not rewrite backups. Backups that hold personal data are kept at most 35 days (`scripts/backup_postgres.py --retention-days 35`), so an erasure reaches every backup within 35 days.
  - **Restores.** After a restore, every erasure recorded since the backup was taken (`privacy.subject_erased` in the security audit log, which is retained) must be applied again before the gateway serves traffic. Today that means repeating each erasure request, each with a new dual approval. Planned (security): a command that re-applies the logged, dual-approved erasures.
  - **Projects.** A project owned by the subject keeps its organization; its `owner_actor` becomes `[erased]`, and the organization's owners can reassign it (gateway-clients' #285).
  - **Notifications.** A notification about an erased record stays visible to its organization with ids only: no text and no actor.

## Reporting

Do not include secrets, credentials, private keys, tokens, customer data, or production evidence payloads in public issues. Use a private security channel controlled by the repository owner.
