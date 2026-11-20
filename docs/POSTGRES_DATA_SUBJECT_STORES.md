# Data-subject requests: PostgreSQL runtime stores

`privacy.data_subject.DataSubjectService` (security, #270) calls
`export_subject(actor)` and `erase_subject(actor)` on each store the runtime
holds. On PostgreSQL deployments, the approval store, run store, job queue,
outbound messages and memory are the twins in `repositories/postgres_state.py`.
They follow the rules of the SQLite stores: security's `ApprovalStore` (#270)
and agent-core's stores (#275, `docs/DATA_SUBJECT_STORES.md`). Matching is by
actor id; for runs and jobs that is the message's `sender_id`.

| Runtime store | Export | Erase |
|---|---|---|
| `rt.approval_store` (`PostgresApprovalStore`) | Approval records the actor requested (`created_by`, `proposer`, `actor`, as `proposal_requesters` reads them) | Deleted, with their dual-approval rows. Someone else's request keeps the record of who approved it, as in the SQLite store |
| `rt.run_store` (`PostgresRunStore`) | Runs the actor asked for: original message, plan, results, pending proposal. Never resume tokens | A finished run is deleted. A run still in flight becomes a `cancelled` tombstone without content: it cannot be resumed, and `update()` leaves it alone, so a loop that is still running cannot write the content back |
| `rt.job_queue` (`PostgresJobQueue`) | Task messages the actor sent, with their results | Deleted. A queued task then never runs |
| `rt.outbound_messages` (`PostgresOutboundMessageStore`) | Messages to the actor, or queued for them (`payload.requested_by`) | Deleted. A queued message is then never sent |
| `rt.memory` (`PostgresExperienceStore`) | Experiences and structured experiences, decrypted; memory governance audit entries that name the actor | Experiences deleted. Governance audit entries stay, as the record of each decision, with the actor replaced by `[erased]` |

Each store erases in one transaction. The rows it reads stay locked until the
commit, so a concurrent update cannot write an erased row back.

## Not covered

- **Dead row versions.** PostgreSQL keeps deleted and updated row versions until
  vacuum reclaims them. The write-ahead log and backups keep them until they are
  recycled or rotated out.
- **Other runtime state** in the same table, such as break-glass sessions, webhook
  replay guards and idempotency records, is not among the privacy service's
  stores.
- **Learning metrics** are daily totals without an actor.
