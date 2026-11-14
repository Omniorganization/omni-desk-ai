# Dual approval and break-glass: two-person control

This runbook covers how critical approvals and delegated break-glass sessions
get their second person, and what operators must do differently since the
2026-10-22 fix for review findings M2 and M3.

## Dual approval (critical proposals)

- The requester of an approval is recorded server-side when the approval is
  created, in `proposal.created_by` and `proposal.proposer`:
  - `POST /approvals`: the authenticated admin actor. The request body cannot
    set these fields (`extra="forbid"`), and any value is overwritten.
  - Orchestrator (`PermissionManager` in `remote_approval` / `auto_policy`
    mode): the proposal `actor`, which is the authenticated message sender
    (`ToolContext.actor`). A caller-supplied `created_by` is overwritten.
- `POST /approvals/{id}/dual-approve` refuses any approver equal to
  `created_by`, `proposer` or `actor`. For `POST /approvals` this means the
  actor named in the body cannot approve either.
- A proposal that needs dual approval but has no recorded requester is refused.
  The SQLite store refuses before writing anything. On PostgreSQL a direct
  store call leaves a `pending` approval without a dual record, which can never
  be approved; both production paths always record a requester first.
- A caller whose owner role comes from a break-glass session gets `403` from
  `dual-approve`. Two owners authenticated with their own owner tokens are
  needed.

### Limits

- The requester check compares identity strings. A request that arrives
  through a channel records the channel sender id (for example a Telegram user
  id), and the CLI records `owner`. The gateway has no mapping from those ids to
  admin token actors, so a person who sends a critical request from a channel
  and also holds an owner token is not recognised as the requester when they
  dual-approve it. Owners should not approve critical requests they sent from a
  channel; closing this needs an identity mapping (follow-up).

## Break-glass

Break-glass stays disabled unless `permissions.break_glass_enabled: true`.

### Self-targeted session (unchanged)

`POST /admin/break-glass/open` without `target_actor` (or with the caller's own
actor) opens an active, time-boxed session for the calling owner.

### Session for another actor (changed)

1. Owner A requests it:

   ```http
   POST /admin/break-glass/open
   Authorization: Bearer <owner A token>
   Idempotency-Key: <unique>

   {"target_actor": "<actor to elevate>", "reason": "<incident reference>",
    "ttl_seconds": 900, "requires_dual_approval": true}
   ```

   The response has `"requires_second_approval": true` and
   `session.status == "pending_second_approval"`, `session.active == false`.
   The session grants nothing yet. `requires_dual_approval: true` is still
   required in the body; without it the request is refused with `409` as before.

2. A second owner B checks the request (`GET /admin/break-glass/status/{id}`
   shows the target, reason and requester in `approved_by`) and approves it:

   ```http
   POST /admin/break-glass/approve/{session_id}
   Authorization: Bearer <owner B token>
   Idempotency-Key: <unique>

   {"reason": "<optional note>"}
   ```

   B must authenticate with their own owner token. The request is refused
   (`409`) when B is the requester or the target actor, and (`403`) when B's
   owner role itself comes from a break-glass session. On success the session
   becomes `active`, `second_approver` is set, and its TTL starts now.

3. The target uses the session with its own token plus
   `X-OmniDesk-Break-Glass-Session: <session_id>`.

- A pending request must be approved within 15 minutes; after that it can only
  be revoked. `POST /admin/break-glass/revoke/{id}` cancels a pending request
  or ends an active session.
- An elevated session cannot open or approve break-glass and cannot provide a
  dual approval. Other owner-level routes keep working with it.
- Store transitions are appended to the break-glass audit log
  (`permissions.audit_log`): `break_glass.request`, `break_glass.approve`,
  `break_glass.open`, `break_glass.revoke`. Each use of a session is recorded
  by AdminAuth as `break_glass.use` in `<workspace.root>/admin_auth_audit.jsonl`.
- The identity of the opener and of the second approver comes only from the
  authenticated token. `X-OmniDesk-Actor` is no longer used as a fallback on
  `open`, `approve` or `dual-approve`.

## Impact on existing callers

- Production has at most two owner identities (`OMNIDESK_OWNER_TOKEN` and
  `OMNIDESK_ADMIN_TOKEN`). A critical request started with one of them, through
  `/agent/run` or `POST /approvals`, or a `POST /approvals` whose `actor` names
  one of them, leaves only one eligible owner and can never reach dual
  approval. Start critical work with the operator token (or from a channel) so
  that both owners can approve it. Before the fix this only worked because the
  requester could approve their own request.
- Scripts that open a delegated session and use it straight away now get a
  pending session; using it returns `403 break-glass denied: ... not active`
  until a second owner approves it.
- A deployment with a single owner identity cannot delegate break-glass. It
  needs a second owner identity (for example `OMNIDESK_OWNER_TOKEN` and
  `OMNIDESK_ADMIN_TOKEN` with distinct `*_ACTOR` values, held by different
  people). If only one owner is reachable during an incident, that owner acts
  with their own token instead of delegating.
- Delegated sessions opened before the upgrade become `legacy_unapproved` and
  stop working (SQLite: schema migration; PostgreSQL: on read). Self-targeted
  sessions keep working until they expire.
- Responses from `open` and `status` now also include `status` and
  `second_approver` in `session`, and `open` returns `requires_second_approval`.
- `storage.backend: postgres` behaves the same: the PostgreSQL break-glass and
  dual-approval stores use the same checks as the SQLite ones. PostgreSQL rows
  are JSON, so there is no schema migration; a stored session without a
  `status` field is read as `legacy_unapproved` when it is delegated and as
  `active` when it is self-targeted.

## Upgrading or rolling back several instances

During a rolling deploy, instances on the previous version ignore `status` and
the second-approver rule: they accept single-owner delegation and treat a
pending request as active. Either deploy with
`permissions.break_glass_enabled: false` on every instance and enable it once
all instances run the same version, or accept that delegation is not two-person
controlled until the rollout finishes.

Before rolling back, revoke every session the previous version would wrongly
treat as active: pending requests and delegated sessions from before the
upgrade. Find them, then call `POST /admin/break-glass/revoke/{session_id}` for
each (revoke works while break-glass is disabled):

```sql
-- SQLite (<workspace.root>/break_glass.sqlite3)
SELECT session_id FROM break_glass_sessions
WHERE revoked_at IS NULL AND status IN ('pending_second_approval', 'legacy_unapproved');

-- PostgreSQL
SELECT key FROM omnidesk_core_state
WHERE namespace = 'break_glass_sessions' AND value_json->>'revoked_at' IS NULL
  AND (value_json->>'status' = 'pending_second_approval'
       OR (value_json->>'status' IS NULL AND value_json->>'actor' <> value_json->>'approved_by'));
```

## Rollback

1. Preferred mitigation if the new flow blocks incident response: use the
   owner's own token, or set `permissions.break_glass_enabled: false`. Do not
   roll back only to restore single-person delegation; that reopens M3.
2. Code rollback: revert the commit. The SQLite migration only adds nullable
   columns (`status`, `second_approver`, `approved_at`, `ttl_seconds`), and the
   PostgreSQL rows only gain JSON fields; the previous code ignores both, so no
   data migration is needed. First revoke the sessions listed above: the
   previous code treats every unrevoked, unexpired row as active, including
   pending requests and `legacy_unapproved` delegated sessions.
3. After a later re-upgrade, rows written by the previous code are classified
   again: self-targeted rows stay active, delegated rows become
   `legacy_unapproved`.
