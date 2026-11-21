# Alert runbook

Every alert in `deploy/observability/prometheus-rules.yml` and
`deploy/observability/prometheus-rules.host.yml` links here. Alertmanager
(`deploy/observability/alertmanager.base.yml`, rendered by
`scripts/render_alertmanager_config.py --receiver slack`) sends them straight
to the on-call Slack channel through an incoming webhook, not through the
gateway. The webhook URL is the secret `OMNIDESK_ALERT_SLACK_WEBHOOK_URL`; it
is mounted as a file and never written into the config. `critical` alerts repeat every hour and
`warning` alerts every 12 hours until resolved.

SLOs and error budgets are defined in [docs/SLO.md](../SLO.md).

## Incident basics

1. Acknowledge in the on-call channel. State who owns the incident.
2. Check `GET /health`, `GET /ready` and the Grafana "OmniDesk runtime" dashboard.
3. If a deploy happened in the last hour, roll back first and investigate second
   (`.github/workflows/rollback-drill.yml` documents the procedure).
4. After the alert resolves, write a short note: start, detection, mitigation,
   end, customer impact, and follow-up issues.

## OmniDeskGatewayDown

Prometheus cannot scrape `/admin/metrics` on a gateway instance for 2 minutes.
- `curl -fsS https://<host>/health`. If it fails, the process is down: check
  `docker compose ps` or `kubectl get pods`, then the last 200 log lines.
- If `/health` works, the scrape is being refused: the viewer token in
  `omnidesk_metrics_token` was rotated or the NetworkPolicy blocks Prometheus.

## OmniDeskGatewayAbsent

Prometheus has no `omnidesk-agent` target at all, so every gateway alert is blind.
Fix the scrape configuration or ServiceMonitor before anything else.

## OmniDeskAvailabilityBudgetFastBurn

More than 7.2% (1h and 5m windows) or 3% (6h and 30m windows) of API requests
are failing with 5xx or unhandled exceptions. At that rate the 30-day 0.5%
error budget is exhausted in about 2 or 5 days.
- Correlate with the latest deploy (`artifact_sha256` in `/health`).
- Check `/ready`: Postgres, the sandbox runner and the job queue.
- Look at `omnidesk_http_requests_total` by `path` to find the failing route.

## OmniDeskAvailabilityBudgetSlowBurn

The error ratio is above 1.5% over a day or 0.5% over three days. This is not
urgent, but the monthly objective will be missed. Open an issue with the top
failing routes.

## OmniDeskLatencyBudgetFastBurn

More than 14.4% (1h) or 6% (6h) of control-plane requests take longer than 1s.
Model-bound routes (`/api/chat*`, `/agent/run`, conversation `ask`) are excluded.
- Synchronous Postgres calls on the event loop (rate limiter, auth) are the
  usual cause. Check Postgres CPU and connection count.
- Check host CPU steal on shared-vCPU hosts.

## OmniDeskToolCallLatencyHigh

The p95 duration of `tool.call` spans is over 30 seconds for 15 minutes.
Check the sandbox runner (queue, image pulls, CPU), and the model provider
latency for model-backed tools. The span histogram is
`omnidesk_span_duration_seconds{span="tool.call"}`.

## OmniDeskApprovalResumeFailures

Approved runs failed to resume. Look for expired or replayed resume tokens and
for runs stuck in `resuming` (`/admin/status` → `resume_recovery`).

## OmniDeskOutboundAmbiguousSend

A provider send failed in a way that may or may not have delivered the message.
Check the provider (message id, chat history) before requeueing, otherwise the
customer may receive the message twice.

## OmniDeskOutboundDeadLetterRate

Outbound messages exhausted their retries. Check provider credentials, the
recipient and rate limits; requeue only after the cause is fixed.

## OmniDeskJobsDeadLettered

Background jobs exhausted their retries. Inspect `/admin/jobs/dead-letter`;
requeue only after fixing the cause.

## OmniDeskWebhookSignatureFailures

Inbound webhooks failed signature, verification-token or replay checks. A
burst after a secret rotation is expected; otherwise treat it as a probe and
check the source IPs in the access log.

## OmniDeskWebhookEnqueueFailures

Verified inbound messages could not be written to the job queue. Customer
messages may be lost, so treat this as an outage: check Postgres health and disk.

## OmniDeskRequestsThrottled

More than 20% of requests are rejected by admission control (429). If all
users sit behind one ingress, set `api_resource_guard.trusted_proxy_ips` so
quotas apply per client, not per ingress IP.

## OmniDeskTraceSpanFailures

Traced operations failed. Approval pauses are not counted. Use the `trace_id`
from the JSON logs to find the failing tool call or job.

## OmniDeskTraceExportFailures

Spans are not reaching the OTLP collector. `reason="http_4xx"` means the
collector rejected the payload (a regression in the exporter; the
`Observability Conformance` workflow should have caught it). `connection` and
`timeout` mean the collector is unreachable: check `OMNIDESK_OTLP_ENDPOINT`,
the collector pod and the NetworkPolicy egress to port 4318.

## OmniDeskTraceSpansDropped

The in-process export queue overflowed, so the oldest spans were dropped. This
usually follows a slow or unreachable collector.

## OmniDeskMetricSeriesDropped

The metrics registry reached its series cap and discards new label
combinations, so alerts may lose their signal. Find the metric with an
unbounded label (user ids, raw paths) in `/admin/metrics`.

## OmniDeskNodeExporterDown

Host-level alerts (disk, memory, clock, backups) are blind. Restart
node_exporter and check that its textfile directory is mounted.

## OmniDeskHostDiskFillingUp

Less than 15% disk is free. Prune unused images (`docker image prune`) and
backups older than the retention period. Never delete the Postgres volume.

## OmniDeskHostDiskCritical

Less than 5% disk is free and Postgres writes will fail soon. Free space now,
then plan a larger disk.

## OmniDeskHostMemoryLow

Less than 10% memory is available. Check for a leaking container
(`docker stats`) and the number of concurrent sandbox runs.

## OmniDeskHostClockNotSynchronised

The clock is not NTP-synchronised. Signed runner requests, device signatures,
approval expiry and TLS all depend on it. Check `timedatectl` and that UDP 123
egress to the configured NTP servers is allowed by the cloud firewall.

## OmniDeskBackupMissing

No backup on this host has passed verification yet. Check the backup timer's
journal, that `verify_backup.py` runs with `--metrics-file` pointing at
`omnidesk_backup.prom` in the node_exporter textfile directory
(`docs/BACKUP_RESTORE.md`), and that node_exporter reads that directory.

## OmniDeskBackupStale

A backup kind missed its schedule: PostgreSQL (daily, RPO 24 hours) has had no
verified backup for 26 hours, or SQLite (hourly, RPO 1 hour) for 2 hours. The
`kind` label says which. Run that backup unit by hand and read its journal.

## OmniDeskBackupVerifyFailure

`verify_backup.py` rejected the latest backup set: a checksum, decryption,
integrity (`PRAGMA quick_check`, `pg_restore --list`) or trial-restore failure.
Keep the previous verified set, and fix the backup before the next scheduled run.
