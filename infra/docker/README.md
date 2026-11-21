# Docker

Source assets:

- `Dockerfile`
- `deploy/docker/docker-compose.yml`
- `deploy/docker/docker-compose.full.yml`
- `deploy/sandbox-runner/Dockerfile`

Release images must be digest-pinned in downstream deployment evidence before customer distribution.

## Backups

The compose stacks schedule no backups. In `docker-compose.full.yml`,
PostgreSQL publishes no port and sits on the internal `omnidesk-private`
network, so the single-host backup units in `deploy/systemd/` do not reach it
as shipped. Those units are `omnidesk-backup.service`/`.timer` (SQLite) and
`omnidesk-postgres-backup.service`/`.timer` (PostgreSQL through host
`pg_dump`, from #255), both keeping 30 days; `docs/BACKUP_RESTORE.md` has the
commands. A compose deployment therefore needs its own scheduled backup, and
it follows the same rules (SECURITY.md: erasure does not rewrite backups):

- keep backups 30 days by default and never more than 35;
- after any restore, the instance serves no traffic until every erasure logged
  since the backup was taken (`privacy.subject_erased`) has been applied
  again: with security's re-apply command once it ships, and until then by
  repeating each erasure request with a new dual approval.
