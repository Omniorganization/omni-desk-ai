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
| `rt.approval_store` (`PostgresApprovalStore`, with `PostgresDualApprovalStore`) | Approval records the actor requested, in full (`created_by`, `proposer`, `actor`, as `proposal_requesters` reads them); the actor's decisions on others' requests as `{id, role: "decider", status, decided_at, result}` | Security's one rule for approvals in every store (`SECURITY.md`): records stay, without the person. In a request by the actor, requester fields become `[erased]`, only structural fields stay (`STRUCTURAL_REQUEST_KEYS`; any other field, such as a planner's `reason` or `summary`, is dropped and named in `fields_withheld`), and `args` keeps only `*_sha256`/`*_length` keys (plus `args_withheld`); a pending one is withdrawn (`denied`, `result.withdrawn = "requester erased"`). In a decision by the actor, the decider becomes `[erased]` and the reason they wrote is dropped. A pending dual approval loses the actor's lone first approval, so two fresh approvers are needed; in a ready one the actor becomes `[erased]` |
| `rt.run_store` (`PostgresRunStore`) | Runs the actor asked for: original message, plan, results, pending proposal. Never resume tokens | A finished run is deleted. A run still in flight becomes a `cancelled` tombstone without content: it cannot be resumed, and `update()` leaves it alone, so a loop that is still running cannot write the content back. `get()` reports `erased: true` for it (`false` otherwise), so the orchestrator writes no memory about it |
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
