# Backup and Restore

Single-node deployments keep runtime state in SQLite files under the workspace
(`runs`, `approvals`, `dual_approvals`, `break_glass`, `webhooks`, `jobs`,
`outbound_messages`, `agent_run_idempotency`, `side_effect_idempotency`,
`learning_experiments`, `memory`, `token_budget`, `model_costs`, and more as
features are enabled). Back them all up with `--discover <state dir>` rather than
a list that drifts; OAuth token files under `google/` are separate.

Runtime logs and screenshots can be retained separately according to your retention policy.

## Encrypted Drill

Set `OMNIDESK_BACKUP_ENCRYPTION_KEY` to a secret value of at least 32 characters, then run:

```bash
python scripts/backup_sqlite.py --dest backup --encrypt --retention-days 30 memory.sqlite3 approvals.sqlite3 runs.sqlite3
python scripts/verify_backup.py backup/backup_manifest.json --require-encryption
python scripts/restore_sqlite.py backup/runs.sqlite3.<timestamp>.bak.enc restored-runs.sqlite3 --force --encrypted
```

The manifest records encrypted file hashes plus plaintext hashes. Verification decrypts to a temporary SQLite file and runs `PRAGMA quick_check` before restore. Restore keeps a `.pre-restore-<time>` copy of the replaced file and moves its `-wal`/`-shm` files beside that copy: left in place, a stale WAL would be replayed onto the restored database. Stop the daemon before restoring.

`deploy/systemd/omnidesk-backup.service` backs up every `*.sqlite3` in `/var/lib/omnidesk` (`--discover`).

## PostgreSQL backup / restore

Production keeps multi-instance state in PostgreSQL: the core runtime state
(`OMNIDESK_POSTGRES_DSN`) and AppSync (`OMNIDESK_APPSYNC_POSTGRES_DSN`). The
backup takes both by default (once, if they are the same database). The logic is
in `omnidesk_agent/storage/postgres_backup.py`; it needs the PostgreSQL client
tools (`pg_dump`, `pg_restore`; on PATH or in `OMNIDESK_PG_BIN`) of the server's
major version.

```bash
export OMNIDESK_BACKUP_ENCRYPTION_KEY=...   # at least 32 characters
export OMNIDESK_BACKUP_MANIFEST_KEY=...     # at least 32 characters
python scripts/backup_postgres.py --dest /var/backups/omnidesk-pg --encrypt --sign-manifest --keep 14 \
  --metrics-file /var/lib/node_exporter/textfile_collector/omnidesk_backup.prom
```

Each run writes a backup set `<dest>/<UTC time>-<id>/` with one `pg_dump`
custom-format archive per database and `manifest.json`. The manifest records
each archive's SHA-256 (and the plaintext's), its table-of-contents size, the
AppSync schema versions, and the exact row count of every table. The counts and
the dump read the same exported snapshot, so writes during the backup cannot make
a correct restore look wrong. Archives are encrypted with AES-256-GCM in
authenticated chunks (HKDF-SHA256 key, per-file salt; truncation and reordering
are detected). The new set is verified before the command succeeds, and
`--keep`/`--retention-days` prune older sets. Passwords reach `pg_dump` through
`PGPASSWORD`, never its command line.

Verify a set (checksums, decryption, table of contents), optionally with a full
trial restore into a scratch database:

```bash
python scripts/verify_backup.py <set>/manifest.json --require-encryption --require-manifest-signature \
  --metrics-file /var/lib/node_exporter/textfile_collector/omnidesk_backup.prom
OMNIDESK_SCRATCH_DSN=postgresql://.../omnidesk_restore_check \
python scripts/verify_backup.py <set>/manifest.json --restore-into-dsn-env OMNIDESK_SCRATCH_DSN --confirm-database omnidesk_restore_check
```

`--metrics-file` keeps `omnidesk_backup_verify_failure_total{kind=...}` (the
counter the `OmniDeskBackupVerifyFailure` alert watches) and
`omnidesk_backup_last_verify_success_timestamp_seconds` in a node-exporter
textfile.

Restore one database into a prepared, empty database:

```bash
OMNIDESK_RESTORE_POSTGRES_DSN=postgresql://.../omnidesk_appsync \
python scripts/restore_postgres.py --manifest <set>/manifest.json --database appsync \
  --confirm-database omnidesk_appsync --require-signature --report restore-report.json
```

The target comes from its own variable, so a restore never defaults to the live
database; `--confirm-database` must name it; a target that already has tables is
refused unless `--clean`. `pg_restore` runs with `--single-transaction
--exit-on-error`, then every table's row count and the AppSync schema versions
are compared with the manifest. The JSON report records `restore_seconds` (RTO)
and `backup_age_seconds` (the RPO of that restore) for drill evidence.

After a restore, deploy the matching signed OmniDesk image digest and run
`/ready` before admitting traffic. For point-in-time recovery, run PostgreSQL
with WAL archiving at the managed database or cluster layer. Scheduling the
backup and running restore drills on staging belong to the deployment; this
repository records no drill result as evidence.
