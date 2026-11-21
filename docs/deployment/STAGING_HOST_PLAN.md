# Staging host plan (shared host or new host)

Status: **plan only**. The owner deferred the staging host until every other
part is finished. Nothing is provisioned, no host is chosen, no public endpoint
is open, and no host secret has been requested. The host step runs last.

What staging must run: the latest signed **server release** from
`release.yml`'s `server-release` job: the gateway image built from the signed
wheel, the sandbox-runner image, Postgres 16, TLS ingress, Prometheus and
Alertmanager (to Slack). Real drills then run against it and their raw outputs
become evidence.

## Variant A: a server the owner already runs other programs on (preferred)

This starts from the web-first single-host path (`scripts/web_first_host.py`,
`deploy/web-first/`): systemd units plus rootless Podman, exercised in CI by
`web-first-candidate.yml` against real Postgres, Podman and a browser.

### Step 0: read-only inventory (no installs, no changes)

Run as the owner's admin user, and share the output before any decision:

```bash
cat /etc/os-release; uname -r; nproc; free -h; df -h /
ss -tlnpu                         # who holds 80/443/51820 and other ports
systemctl list-units --type=service --state=running --no-pager
command -v docker podman nginx apache2 caddy || true
sudo nft list ruleset | head -100; sudo ufw status verbose || true
sysctl kernel.apparmor_restrict_unprivileged_userns user.max_user_namespaces || true
timedatectl
```

### Isolation rules

- **One dedicated user** (`omnidesk`) owns everything: rootless Podman
  (`podman` with `pasta`), systemd user units with linger. No new Docker
  daemon and no iptables rewrite. Docker is used only if the host already runs
  it, and then never through its socket.
- **Resource caps** on that user's slice, sized after the inventory, for
  example `MemoryMax=3G`, `CPUQuota=150%`, `TasksMax=2048`. Per-container
  `--memory`/`--cpus` limits sit inside it.
- **Egress per OmniDesk, not host-wide.** All OmniDesk traffic leaves as the
  `omnidesk` UID (rootless Podman), so one dedicated nftables table
  (`table inet omnidesk`, `meta skuid omnidesk`) can allow loopback, DNS, NTP
  and HTTPS to the owner-approved provider destinations and drop the rest. It
  leaves every other table, firewall manager (ufw/firewalld) and program
  untouched. Alternative: put the gateway on an internal Podman network whose
  only way out is an allowlisting HTTPS proxy container.
- **SSH and the existing firewall stay as they are.** CI reaches the host over
  WireGuard (UDP 51820, only if free) or an SSH key restricted to the
  `omnidesk` user with a forced command. Deploys need no root and no sudo
  (rootless), unlike variant B.
- **Ingress:** if a web server already holds 80/443, add one site (vhost)
  that proxies `https://<staging host name>` to the gateway on
  `127.0.0.1:18789`, using that server's existing ACME/TLS setup. Bind nothing
  new to 80/443 in that case. Only if 80/443 are free, run Caddy with the
  owner's approval.
- **Postgres** runs as a rootless Podman container with its volume in the
  `omnidesk` home. Backups cover only OmniDesk's database and config, and are
  kept 30 days, never more than 35 (see "Backups and restores" below).
- **Drills touch only OmniDesk:**
  - soak: at most 4 requests/s from one client, the gateway's own per-IP quotas;
  - failure injection and rollback: only OmniDesk units and containers;
  - maintenance: `VACUUM (ANALYZE)` of OmniDesk's database only;
  - restore drill: into a scratch database in OmniDesk's Postgres, never the live one.

## Variant B: a new dedicated host

This is the reference implementation in the parked draft PR #249: docker
compose (`deploy/docker/docker-compose.full.yml` plus a staging overlay with
Caddy, node_exporter, Prometheus and Alertmanager), an idempotent bootstrap, and
a forced-command deploy key that can run only a root helper. That helper
deploys by digest with automatic rollback, smoke-tests, backs up, runs the
restore drill and does maintenance. The design is rehearsed on a disposable CI
VM. Known fixes before reuse:
- `systemctl --user` for the runner user needs its bus address
  (`DBUS_SESSION_BUS_ADDRESS`) through sudo.
- Its backup runs `pg_dump` itself and keeps the newest 7 dumps with no age
  limit. Replace it with `scripts/backup_postgres.py --retention-days 30`
  (`tests/test_backup_retention_policy.py` fails on it until then).

Host-level points that also apply to any dedicated VM:
- The host firewall must allow **NTP (UDP 123)**. Otherwise the clock drifts
  and signed sandbox-runner requests, device signatures and approval expiry fail.
- Egress allowlist destinations change: GitHub/GHCR (from
  `https://api.github.com/meta`), the model provider's published ranges, Slack,
  and the ACME endpoint. Review them at apply time; never fall back to
  allow-all to hide a failure.
- Hetzner prices on 2026-11-21: CX23 EUR 7.09/month with backups (then "not
  available"); CPX22 HEL1 about EUR 20.4/month. Closed PR #254 has the
  Terraform changes if this variant is chosen.

## Common to both variants

- **Release input:** `server-release-dist` from `release.yml` (PR #263).
  `deploy-staging` verifies payload signatures and attestations, checks the
  image digests against the signed metadata, and verifies both images'
  signatures and attestations before deploying.
- **Secrets** the owner sets later with `gh secret set NAME --env staging`
  (names only, never in chat):
  - `OMNIDESK_ALERT_SLACK_WEBHOOK_URL`: Slack incoming webhook for the alerts channel;
  - `OMNIDESK_STAGING_SSH_PRIVATE_KEY`: CI deploy key (and
    `OMNIDESK_STAGING_WG_PRIVATE_KEY` if WireGuard is used);
  - the staging model API key; its name follows the chosen provider.
  Variables: `OMNIDESK_STAGING_HOST`, `OMNIDESK_STAGING_SSH_KNOWN_HOSTS`,
  `OMNIDESK_STAGING_BASE_URL`, `OMNIDESK_STAGING_IMAGE_REPOSITORY` (and
  `OMNIDESK_STAGING_WG_ENDPOINT`, `OMNIDESK_STAGING_WG_HOST_PUBLIC_KEY`).
- **Generated on the host, never leaving it:** `OMNIDESK_ADMIN_TOKEN`,
  `OMNIDESK_VIEWER_TOKEN`, `OMNIDESK_GATEWAY_SECRET`,
  `OMNIDESK_APPSYNC_SECRET_PEPPER`, `OMNIDESK_PLUGIN_SIGNING_SECRET`,
  `OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY` (required in production at all times),
  `OMNIDESK_MEMORY_ENCRYPTION_KEY`, `OMNIDESK_SANDBOX_RUNNER_TOKEN`,
  `OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET`, and the Postgres password.
- **Backups and restores** (privacy rules in SECURITY.md):
  - A daily timer (the PostgreSQL backup unit and timer from #255) runs
    `scripts/backup_postgres.py --encrypt --sign-manifest --retention-days 30
    --metrics-file <node_exporter textfile dir>/omnidesk_backup.prom`.
    Erasure does not rewrite backups, so backups holding personal data are kept
    30 days, the same default as the SQLite unit, and never more than 35. The
    scripts default to `--retention-days 0` (keep everything), so the flag is
    required; `tests/test_backup_retention_policy.py` fails on a scheduled
    backup without it or above 35.
    `web_first_host.py backup` stays a manual one-off: it never deletes old dumps.
    RPO on the host: up to 24 hours for PostgreSQL (one dump a day) and about
    1 hour for the SQLite stores (hourly timer). Nothing on the host is faster.
  - After every restore, the drill included, the restored gateway serves no
    traffic until each erasure logged since the backup was taken
    (`privacy.subject_erased` in the security audit log) has been applied
    again. Use security's re-apply command once it exists; until then, repeat
    each erasure request with a new dual approval. The drill records how many
    erasures it re-applied.
- **Public webhook endpoint** for the channels session's live WhatsApp tests:
  the gateway's webhook routes behind the same TLS host name, opened only
  after the owner approves the host. Its host name goes to the coordinator.
- **Self-healing** (autonomy): a unit or timer for
  `python -m omnidesk_agent.self_healing.watchdog`, plus a window for the first
  real failure-injection drill (bad-config deploy, then watchdog rollback and
  restart, then a health check). No evidence file is written before that real
  run.

## Final-phase order

1. Owner chooses the variant and approves cost and access. Run the inventory (variant A).
   The owner also decides on point-in-time recovery for the host. Continuous
   WAL archiving (pgBackRest or wal-g) to storage off the host would cut the
   PostgreSQL RPO from up to 24 hours to minutes. The storage is the owner's
   choice and may cost money; it is not built.
2. Bootstrap with the reviewed commit. Deploy the latest server release and run the smoke with strict sandbox.
3. Send the TLS host name for the channels webhooks to the coordinator.
4. Run the drills on the host and keep raw outputs with run URLs: deploy,
   rollback (including a failed rollout), backup and restore with measured
   RPO/RTO (logged erasures re-applied before the restored instance serves
   traffic), alert delivery to Slack, maintenance, product soak and the
   Postgres multi-instance soak, and the self-healing drill.
5. Import only real outputs as evidence. Never hand-edit evidence, and never
   weaken the external GA evidence gate.
