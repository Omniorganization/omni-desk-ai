# Data-subject requests: agent-core stores

`privacy.data_subject.DataSubjectService` (the security session's export and dual-approved erasure) calls `export_subject(actor)` and `erase_subject(actor)` on each store the runtime holds. The SQLite stores below implement both. They match the actor id on every channel; for runs and jobs, that is the message's `sender_id`.

| Runtime store | Export | Erase |
|---|---|---|
| `rt.memory` (`ExperienceStore`) | Experiences, structured experiences and interaction profiles, decrypted; memory governance audit entries that name the actor | Rows deleted, with their embeddings. Both full-text indexes are rebuilt: they index external content and would otherwise keep the words. Governance audit entries stay, as the record of each decision, with the actor replaced by `[erased]` |
| `rt.run_store` (`RunStore`) | Runs the actor asked for: original message, plan, results, pending proposal. Never resume tokens | A finished run is deleted. A run still in flight becomes a `cancelled` tombstone without content (see below) |
| `rt.outbound_messages` (`OutboundMessageStore`) | Messages addressed to the actor, or queued for them (`payload.requested_by`) | Deleted. A queued message is then never sent |
| `rt.job_queue` (`JobQueue`) | Task messages the actor sent, with their results | Deleted. A queued task then never runs |

## How an erasure stays erased

- **In-flight runs.** The tombstone stops the run at its next step or turn, because the loop reads the run's status. Nobody can resume it. `update()` ignores the row, so a loop that is still running cannot write the content back.
- **Database files.** Each erasure runs in one `IMMEDIATE` transaction with `secure_delete`, which zeroes the content it frees. `VACUUM` then rewrites the file without free pages left by earlier ordinary updates. Finally the write-ahead log is checkpointed and truncated. If another connection keeps `VACUUM` from running, the rows are still gone and their pages zeroed; a warning is logged, and free pages from before the erasure stay until the next `VACUUM`.
- **Who sent a message.** `channels.send_text` now records the requesting actor in the queued payload (`requested_by`). Messages queued before that change are found only through their recipient.

## Not covered

- **Mentions in other people's records.** Text about the person inside someone else's records is not found: matching is by actor id.
- **PostgreSQL stores** in `repositories/postgres_state.py` need their own methods.
- **Work already running.** A step running at the moment of erasure finishes. So does a job a worker is processing, and its run may be created afterwards; erase again if a run appears.
  - A run that finishes after its row was erased writes no memory or learning record. The orchestrator reads the row back after its last write and skips both when the row is a tombstone or gone. `DataSubjectService` erases runs and jobs before the other stores.
- **Other data.**
  - The model response cache has no actor. Its entries are no longer served after `cache_ttl_seconds` (default 24 h).
  - Screenshots in the computer tool's screenshot directory are not linked to an actor and are not erased.
