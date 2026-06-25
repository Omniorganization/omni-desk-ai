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

`POST /app/sync` accepts uploaded `operations` and downloaded `remote_events`, records inbox/outbox dedupe, updates cursors, and opens conflicts for idempotency payload mismatches. Conflicts require manual review unless a reviewed merge strategy is implemented.

The reconnect worker always syncs outbox first and only then calls the update checker. If upload fails or conflicts remain open, update checking is skipped.

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
