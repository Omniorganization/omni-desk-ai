# OmniDesk Production SLOs

These SLOs define the minimum evidence needed before OmniDesk is treated as a long-running public production runtime.

| Area | Target |
| --- | ---: |
| Webhook enqueue success rate | >= 99.9% |
| Job dead-letter rate | < 0.1% |
| Approval resume success rate | >= 99% |
| Planner fallback rate | < 5% |
| Tool error rate | < 2% |
| Outbound send duplicate rate | 0 |
| Plugin timeout rate | < 1% |

## Service-level objectives (Prometheus, 30-day windows)

| Objective | Target | Error budget | Measured by |
| --- | ---: | ---: | --- |
| API availability: requests that do not end in 5xx or an unhandled exception (probes and the metrics scrape excluded) | 99.5% | 0.5% (about 3.6 h of full outage) | `omnidesk:api_error_ratio:*` recording rules |
| Control-plane latency: requests finishing within 1s (model-bound chat/agent routes excluded) | 99% | 1% | `omnidesk:api_slow_ratio:*` recording rules |

Alerts use multi-window burn rates (`deploy/observability/prometheus-rules.yml`):
a 14.4x burn over 1h and 5m, or 6x over 6h and 30m, pages (`critical`);
3x over 1d and 2h, or 1x over 3d and 6h, opens a ticket (`warning`).
`promtool test rules deploy/observability/prometheus-rules.test.yml` pins this
behaviour, and `docs/runbooks/ALERTS.md` lists the response for every alert.

Error budget policy: when less than 25% of the 30-day availability budget is
left, only fixes and rollbacks are promoted to production until the budget recovers.

## Recovery objectives (RPO and RTO)

Only numbers that the code or a drill backs are stated here.

| Deployment | RPO (writes that can be lost) | RTO (time until it serves again) |
| --- | --- | --- |
| Single host, PostgreSQL | Up to 24 hours: one verified logical dump a day (`omnidesk-postgres-backup.service` and its timer, #255). `OmniDeskBackupStale` pages at 26 hours. | Not measured yet. The restore drill on the real host (final server phase) records it. |
| Single host, SQLite stores | About 1 hour: `omnidesk-backup.timer` runs hourly, with up to 5 minutes of random delay. `OmniDeskBackupStale` pages at 2 hours. | Not measured yet, as above. |
| Kubernetes | Set by the external Postgres operator or managed service, not by OmniDesk. Requirement on it: continuous WAL archiving with point-in-time recovery and an RPO of 5 minutes or less. | Requirement on the operator: the database restored within 30 minutes. The OmniDesk steps after that (deploy the matching release, re-apply logged erasures, smoke test) are not measured yet. |

The operator shows its numbers in its own restore drill. Lowering the
single-host RPO needs continuous WAL archiving (pgBackRest or wal-g) to storage
off the host. That is an owner decision for the final server phase and may cost
money; it is not built.

## Runtime ratio snapshot

`GET /admin/slo` returns the current runtime SLO snapshot. The endpoint is protected by viewer-level AdminAuth.

Release and canary automation should treat critical SLO violations as promotion blockers.
