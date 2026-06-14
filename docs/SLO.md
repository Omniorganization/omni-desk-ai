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

`GET /admin/slo` returns the current runtime SLO snapshot. The endpoint is protected by viewer-level AdminAuth.

Release and canary automation should treat critical SLO violations as promotion blockers.
