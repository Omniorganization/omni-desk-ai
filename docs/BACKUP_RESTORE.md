# Backup and Restore

Back up these files while the daemon is stopped or using SQLite online backup:

- `memory.sqlite3`
- `approvals.sqlite3`
- `runs.sqlite3`
- `webhooks.sqlite3`
- OAuth token files under `google/`

Runtime logs and screenshots can be retained separately according to your retention policy.

## Encrypted Drill

Set `OMNIDESK_BACKUP_ENCRYPTION_KEY` to a secret value of at least 32 characters, then run:

```bash
python scripts/backup_sqlite.py --dest backup --encrypt --retention-days 30 memory.sqlite3 approvals.sqlite3 runs.sqlite3
python scripts/verify_backup.py backup/backup_manifest.json --require-encryption
python scripts/restore_sqlite.py backup/runs.sqlite3.<timestamp>.bak.enc restored-runs.sqlite3 --force --encrypted
```

The manifest records encrypted file hashes plus plaintext hashes. Verification decrypts to a temporary SQLite file and runs `PRAGMA quick_check` before restore.

## PostgreSQL HA backup / restore

GA16 production deployments use PostgreSQL for multi-instance runtime state. Logical backups are created with:

```bash
OMNIDESK_POSTGRES_DSN='postgresql://user:pass@postgres:5432/omnidesk' \
  python scripts/backup_postgres.py --output backups/omnidesk-$(date +%Y%m%d%H%M%S).dump
```

Restore into a prepared database with:

```bash
OMNIDESK_POSTGRES_DSN='postgresql://user:pass@postgres:5432/omnidesk' \
  python scripts/restore_postgres.py --input backups/omnidesk.dump --clean
```

For PITR, run PostgreSQL with WAL archiving enabled at the managed database or cluster layer. The application contract is: restore DB to target time, deploy the matching signed OmniDesk image digest, then run `/ready` before admitting traffic.
