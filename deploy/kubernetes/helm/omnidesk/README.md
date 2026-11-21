# OmniDesk Helm chart

Gateway deployment for Kubernetes: 3+ replicas under the namespace's
`restricted` Pod Security profile, Postgres-backed, with a pre-install /
pre-upgrade migration hook. `.github/workflows/kubernetes-ready.yml` proves on
every relevant change that it reaches Ready on a real (kind) cluster.

## Before `helm install`

1. **Namespace guardrails:** `kubectl apply -f deploy/kubernetes/namespace.yaml -f deploy/kubernetes/networkpolicy.yaml`.
   These give `restricted` Pod Security and a namespace-wide default deny. The
   chart's NetworkPolicies then allow only what the gateway needs.
2. **Secrets:** `deploy/kubernetes/external-secret.yaml` must produce the
   `omnidesk-secrets` Secret with every key it lists. The production validator
   refuses to boot without them, and the migration hook reads
   `OMNIDESK_APPSYNC_POSTGRES_DSN` from it. `deploy/kubernetes/external-secret-metrics.yaml`
   provides the viewer token the ServiceMonitor uses.
3. **Postgres and the OTLP collector** run in namespaces labelled
   `omnidesk.io/network: data-plane` (`networkPolicy.dataPlaneNamespaceLabel`).
4. **Dedicated sandbox runner hosts** (below) are running and registered.

## Sandbox runner: dedicated hosts, never pods in this cluster

Readiness requires the sandbox runner: `/ready` stays 503 until
`http://sandbox-runner:18890/ready` answers. The runner executes untrusted
code in containers and needs a **rootless** container runtime. A pod under the
`restricted` profile cannot provide one, and relaxing Pod Security to run it
in-cluster would lower the sandbox policy (AGENTS.md). Production therefore
runs the runner on **dedicated runner hosts** (VMs or bare metal that run
nothing else), and the chart publishes them in-cluster.

### Prepare a runner host

1. Create a dedicated unprivileged user (e.g. `omnidesk-sandbox`) with
   subordinate UID/GID ranges and rootless Podman. Enable linger and check
   that `podman info --format '{{.Host.Security.Rootless}}'` prints `true`.
   The runner refuses to start on a rootful runtime.
2. Install the runner from the same signed release as the gateway, either the
   `omnidesk-sandbox-runner` image from `release.yml` or the wheel in a venv.
   Run it with `deploy/sandbox-runner.service` and an env file based on
   `deploy/sandbox-runner.env.example`. For a cluster, set:
   - `OMNIDESK_CONTAINER_RUNTIME=podman` (the rootless runtime above);
   - `OMNIDESK_SANDBOX_RUNNER_HOST` to the host's private address that the
     cluster can reach (not `0.0.0.0` on a public interface), and
     `OMNIDESK_SANDBOX_RUNNER_PORT=18890`;
   - `OMNIDESK_SANDBOX_RUNNER_TOKEN` and `OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET`
     to the **same values** as the `omnidesk-secrets` keys of the same names.
     Every request is token-authenticated and HMAC-signed with a nonce.
   - `OMNIDESK_SANDBOX_REQUIRE_HMAC=1`, plus a digest-pinned
     `OMNIDESK_SANDBOX_IMAGE_ALLOWLIST` that includes the gateway's
     `sandbox.docker_image`. Pre-pull those images as the runner user (runs
     use `--pull never`).
3. At the host firewall, allow TCP 18890 only from the cluster's node and pod
   CIDRs.
4. Check from the host: `curl -fsS http://<private-ip>:18890/ready`.

### Register the hosts with the chart

```yaml
sandboxRunner:
  external:
    addresses: ["10.0.20.11", "10.0.20.12"]  # runner hosts' private IPv4 addresses
    port: 18890
```

The chart then renders:
- a selector-less `sandbox-runner` Service plus an EndpointSlice
  (`sandbox-runner-hosts`) with exactly these addresses;
- gateway egress to those addresses on that port and nowhere else on it.

Rendering **fails** when the list is empty or holds anything but IPv4
addresses: an egress rule without destinations would open the runner port to
every IP.

To add or remove a host, change the list and `helm upgrade`. The EndpointSlice
marks every listed address ready, so remove a host from the list before taking
it down.

Verify with:
- `kubectl -n omnidesk get endpointslice sandbox-runner-hosts`;
- deep readiness: `GET /admin/ready` with a viewer token must show
  `"sandbox_runner": true`;
- `scripts/production_smoke_test.py --strict-sandbox --sandbox-url http://<runner>:18890`
  from a machine allowed to reach the runner.

## Backups and restores

This chart runs no backup job, and the gateway image has no `pg_dump`.
PostgreSQL is external, so its backups belong to whatever runs it: the
Postgres operator (CloudNativePG, Crunchy) or the managed service. Erasure does
not rewrite backups, so SECURITY.md sets two rules for them:

- **Retention:** keep backups 30 days by default and never more than 35, so an
  erasure reaches every backup within 35 days. Use a time-based retention:
  keeping the last N backups does not bound their age. With point-in-time
  recovery, the oldest base backup can predate the window by one base-backup
  interval, so keep the window plus that interval within 35 days (for example
  a 30-day window with daily base backups).
- **Restores:** a restored database serves no traffic until every erasure
  logged since the backup was taken (`privacy.subject_erased` in the security
  audit log) has been applied again. Use security's re-apply command once it
  ships; until then, repeat each erasure request with a new dual approval.
- **Recovery objectives:** the operator or service must provide continuous WAL
  archiving with point-in-time recovery, an RPO of 5 minutes or less, and a
  database restore within 30 minutes (RTO), shown in its own restore drill.
  On Kubernetes OmniDesk backs neither number itself (`docs/SLO.md`).

The single-host reference is the systemd units in `deploy/systemd/`:
`omnidesk-postgres-backup.service`/`.timer` for PostgreSQL (one verified
`pg_dump` a day, from #255) and `omnidesk-backup.service`/`.timer` for the
SQLite stores (hourly). Both keep 30 days. Their RPO is up to 24 hours for
PostgreSQL and about 1 hour for SQLite. `docs/BACKUP_RESTORE.md` covers backup,
verification and restore.

## Other values that need a decision

- `image.digest`: the release app image digest (`release.yml`). The chart never uses tags.
- `networkPolicy.httpsEgressCIDRs`: HTTPS destinations for model providers and
  channel APIs. NetworkPolicy cannot match host names, so list the providers'
  published CIDRs. Empty means no internet egress.
- `config.trustedProxyIps`: the ingress controller's pod IPs/CIDRs. Without
  them, all users behind the ingress share one rate-limit bucket.
- `networkPolicy.ingressNamespaceLabel` / `ingressPodLabel` /
  `metricsNamespaceLabel`: who may reach port 18789 (ingress-nginx and Prometheus by default).
