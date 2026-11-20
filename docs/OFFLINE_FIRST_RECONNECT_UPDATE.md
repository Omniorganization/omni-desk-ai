# Offline-First Reconnect Update

OmniDesk supports an offline-first runtime contract:

```text
offline/local_only
  -> online_detected
  -> reconnecting
  -> syncing
  -> update_checking
  -> downloading/verifying/staging
  -> health_check
  -> activate or rollback
```

## Runtime Policy

Set `runtime.offline_mode: true` to force core model tasks to the local Ollama profile:

```yaml
runtime:
  offline_mode: true

models:
  default: local
  routing:
    chat: {primary: local, fallback: []}
    planner: {primary: local, fallback: []}
    code: {primary: local, fallback: []}
    upgrade: {primary: local, fallback: []}
    summarize: {primary: local, fallback: []}
```

At load time and runtime startup, OmniDesk rewrites the core routes to `local`, disables external channel/gmail capabilities, and blocks explicit external model profiles. Local Ollama at `http://127.0.0.1:11434` remains allowed.

## Offline Doctor

Run:

```bash
omnidesk doctor --profile offline-first --config examples/config.yaml
```

The offline doctor checks Ollama, required local models, the offline cache layout, Docker image cache markers, SBOM/signature directories, and the release public key. Missing production evidence remains blocked; the doctor does not fabricate external GA readiness.

Expected cache layout:

```text
~/.omnidesk/offline-cache/
  models/
  wheels/
  npm/
  cargo/
  flutter/
  docker-images/
  release-artifacts/
  sbom/
  signatures/
```

## Local Outbox And Reconnect Sync

When the JSON AppSync store is created with `local_outbox_enabled=True` or the runtime is in offline mode, AppSync events are mirrored into a durable local outbox. Each operation records:

- `operation_id`
- `idempotency_key`
- `created_at`
- `actor`
- `device_id`
- `organization_id`
- `payload_hash`
- conflict strategy
- retry status

`GET /app/sync?since_seq=N` keeps the existing incremental pull contract.

`POST /app/sync` accepts uploaded `operations` and downloaded `remote_events`, records inbox/outbox dedupe and cursors, applies uploaded conversations, messages and tasks, and answers per operation in `uploaded.operations`: `applied`, `unchanged`, `duplicate`, `conflict` (with `conflict_id`), `server_kept`, `client_applied`, `merged` or `rejected`. Approvals, notifications and desktop-runtime tasks are never accepted from an upload.

An upload that names a record the server already has with different content is a conflict, and the request's `conflict_policy` decides it:

| Policy | Effect |
|---|---|
| `manual-review` (default) | nothing is applied; the conflict stays open |
| `server-wins` | the server's record stays; the conflict is recorded as resolved |
| `client-wins` | the uploaded value replaces the server's for conversation titles and for titles of tasks still queued without an approval; messages (append-only) and approved, claimed or desktop tasks are never rewritten, so those conflicts stay open |
| `merge` | needs `base_<field>` (the value the client last saw, e.g. `base_title`); if only one side changed since then, that side wins, otherwise the conflict stays open |

The same policies apply when a downloaded remote event reuses a queued local operation's idempotency key with another payload: `server-wins` drops the local operation, `client-wins` keeps it queued to send again, and the others open a conflict. A re-uploaded operation the server already received is a `duplicate` and is not applied again, so a retry never becomes a conflict.

`GET /app/sync/conflicts?status=open|resolved` lists the caller's own conflicts, and `POST /app/sync/conflicts/{conflict_id}/resolve` with `{"resolution": "keep_server" | "apply_client" | "merged", "payload": {...}}` settles one (409 if it is already resolved; 403 for an approved, claimed or desktop task). Another actor's conflict answers 404.

Inbox dedupe and outbox matching only consider the caller's own records: a remote event id or idempotency key is matched within the authenticated actor and its organization, never against another actor's queued operations.

### Reconnect worker and the daemon's upload target

The daemon's reconnect worker uploads the outbox with `omnidesk_agent/offline/uploader.py` to the remote gateway's `POST /app/sync`:

| Variable | Meaning |
|---|---|
| `OMNIDESK_SYNC_UPLOAD_URL` | remote gateway base URL (default `gateway.public_base_url`); `https` required except for a loopback host |
| `OMNIDESK_SYNC_UPLOAD_TOKEN` | bearer token of the remote actor the queued operations belong to |
| `OMNIDESK_SYNC_UPLOAD_ACTOR` | that actor (default `system`); other actors' operations are not uploaded under this token and stay queued |
| `OMNIDESK_SYNC_CONFLICT_POLICY` | `manual-review` (default), `server-wins`, `client-wins` or `merge` |

Without a URL and token the outbox stays queued and the worker reports that no upload target is configured. Each operation is marked from the gateway's answer: delivered ones `synced`, ones the gateway holds as conflicts `conflict` (resolve them at the gateway; they are not sent again), and ones it refuses `rejected` (terminal; for example events the node queued on its own that are not uploadable). Conflicts no longer fail the sync. The update check runs after a successful upload; it is skipped when the upload fails, and deferred while this node has its own open conflicts. The worker does not download remote events.

## Signed Runtime Updates

Runtime updates use a signed manifest and release slots, not source-branch mutation:

```text
releases/
  1.12.7/
  1.12.8-candidate/
  current -> 1.12.7
  previous -> 1.12.6
  candidate -> 1.12.8-candidate
```

The updater verifies:

- manifest Ed25519 signature
- target version is newer
- release channel policy
- artifact SHA-256
- SBOM SHA-256
- `source_commit`
- external GA evidence status before auto activation
- health check before keeping `current`

If health check fails and `rollback_on_failure` is enabled, `current` is switched back to `previous` and `update.rollback` is appended to the audit log.

## Auto-Activation Boundaries

Allowed automatically:

- update check
- background artifact download
- signature/hash/SBOM verification
- staging install
- health check
- stable/real-ga activation when evidence passes
- rollback to previous slot on health failure

Not allowed automatically:

- modifying or merging source branches
- force-pushing
- privilege escalation
- deleting user data
- bypassing approval gates
- marking missing external GA evidence as passed
- forcing unsigned mobile app installs

Mobile updates remain platform-specific: Android uses Play/enterprise distribution, and iOS must use App Store/TestFlight/MDM or another Apple-signed installation path.
