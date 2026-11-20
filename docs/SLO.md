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

## Runtime ratio snapshot

`GET /admin/slo` returns the current runtime SLO snapshot. The endpoint is protected by viewer-level AdminAuth.

Release and canary automation should treat critical SLO violations as promotion blockers.
