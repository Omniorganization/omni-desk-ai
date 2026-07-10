# Project Store Corruption Recovery

The local JSON project store fails closed when schema, JSON, or checksum validation fails. Detection writes a durable `*.projects.json.corruption.json` marker before the damaged primary file is quarantined. While that marker exists, project list, create, update, and delete operations return HTTP `503` with code `PROJECT_STORE_CORRUPT`, including after process restart.

## Inspect recovery state

An owner can inspect the marker and checksum validation result for all rotated backups:

```bash
python scripts/recover_project_store.py --app-sync-path /path/to/app_sync_state.json
```

The owner-only API equivalent is `GET /admin/projects/store/recovery`. The HTTP response intentionally exposes only the stable corruption code, blocked state, backup validity, project count, checksum, and whether a quarantine exists. Raw exception text and server filesystem paths remain available only to the local administrator CLI and on-host audit/marker inspection.

## Restore a validated backup

Recovery automatically selects the newest checksum-valid backup, restores it atomically, verifies the restored checksum, records an audit event, and removes the marker only after verification succeeds:

```bash
python scripts/recover_project_store.py \
  --app-sync-path /path/to/app_sync_state.json \
  --confirm-recovery
```

To select a specific validated rotation, add `--backup-index 1`, `2`, or `3`. The owner-only API equivalent is `POST /admin/projects/store/recover` with an `idempotency-key` header and optional JSON body `{"backup_index": 2}`.

## Risk and rollback

- Recovery may roll project data back to the selected backup's timestamp. Inspect `project_count` and checksum before confirming.
- Invalid backups are never restored. If no checksum-valid backup exists, the marker remains and all project operations stay blocked.
- The damaged primary is retained as a quarantine file for forensic review; do not copy it back over the recovered primary.
- To roll back a recovery implementation change, revert the code through a reviewed PR. Do not manually delete the corruption marker to force service availability.

## Required evidence

Regression coverage includes restart persistence, list/create/update/delete blocking, checksum-invalid backup rejection, automatic selection of a valid rotation, disk-full preservation of the prior primary, crash-temporary-file tolerance, concurrent writers, and owner-only HTTP recovery.
