# L10 Industrial Learning Observability

This module adds industrial observability for self-learning quality.

## What it measures

- `task_success_rate`
- `experience_reuse_rate`
- `reuse_success_delta`
- `bad_memory_rate`
- `stale_memory_rate`
- `contradiction_rate`
- `permission_bypass_rate`
- `high_risk_misexecution_rate`
- `rollback_success_rate`
- `test_coverage`
- `learning_quality_score`
- `industrial_readiness_score`

## Audit source

Learning events are stored as append-only JSONL:

```text
~/.omnidesk/workspace/learning_audit.jsonl
```

## CLI

```bash
omnidesk learning-l10-report --days 7
omnidesk learning-l10-report --days 7 --format html > learning_dashboard.html
```

## Admin API

```text
GET /admin/learning/report?days=7
GET /admin/learning/dashboard?days=7
```

Both routes require AdminAuth.

## SLO behavior

Missing evidence is not treated as success. The SLO evaluator reports
`missing_data` when a metric cannot be computed, because industrial systems need
evidence rather than assumptions.
