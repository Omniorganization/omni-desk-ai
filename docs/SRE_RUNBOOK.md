# OmniDesk SRE Runbook

## 1. Service ownership

OmniDesk is a local-first multi-channel AI agent gateway. Production incidents are usually caused by webhook delivery retries, model-provider degradation, approval deadlocks, plugin failures, sandbox failures, or local SQLite contention.

## 2. Health checks

Use these checks first:

```bash
curl -fsS http://127.0.0.1:18789/health
curl -fsS -H "Authorization: Bearer $OMNIDESK_ADMIN_TOKEN" http://127.0.0.1:18789/admin/status
curl -fsS -H "Authorization: Bearer $OMNIDESK_ADMIN_TOKEN" http://127.0.0.1:18789/admin/metrics
```

For external environments, run:

```bash
OMNIDESK_SMOKE_BASE_URL=https://agent.example.com \
OMNIDESK_SMOKE_ADMIN_TOKEN="$OMNIDESK_ADMIN_TOKEN" \
python scripts/production_smoke_test.py
```

## 3. High-priority alerts

Investigate immediately when any of these rise above baseline:

- `omnidesk_jobs_dead_lettered_total`
- `omnidesk_approval_required_total` without matching approval decisions
- `omnidesk_tool_calls_total{status="exception"}`
- `omnidesk_plugin_call_total{status!="ok"}`
- HTTP 5xx from `omnidesk_http_errors_total`

## 4. Webhook queue recovery

List jobs:

```bash
curl -H "Authorization: Bearer $OMNIDESK_OPERATOR_TOKEN" \
  "http://127.0.0.1:18789/admin/jobs?status=dead_letter"
```

Requeue one dead-letter job:

```bash
curl -X POST -H "Authorization: Bearer $OMNIDESK_OPERATOR_TOKEN" \
  "http://127.0.0.1:18789/admin/jobs/dead-letter/<job_id>/requeue"
```

Purge one dead-letter job after manual triage:

```bash
curl -X DELETE -H "Authorization: Bearer $OMNIDESK_OWNER_TOKEN" \
  "http://127.0.0.1:18789/admin/jobs/dead-letter/<job_id>"
```

## 5. Approval deadlock triage

Symptoms:

- `/agent/resume/<run_id>` returns `waiting_approval` repeatedly.
- The same `approval_id` or same `scope_hash` appears in the audit log.

Actions:

1. Confirm the approval status is `approved`.
2. Confirm the resume token has not already been consumed.
3. Check that the proposal `run_id`, `plan_id`, `step_index`, and `scope_hash` match the waiting run.
4. Resume once. The runtime grants the exact approved scope before re-executing the blocked step, preventing a duplicate approval loop.

## 6. Sandbox incidents

Production requires Docker sandboxing:

- `sandbox.backend=docker`
- `sandbox.docker_network=none`
- plugin sandbox defaults to Docker

If Docker is unavailable, fail closed rather than falling back to host argv execution.

## 7. Release checklist

Before promotion:

```bash
python -m compileall omnidesk_agent
pytest --cov=omnidesk_agent --cov-report=json --cov-fail-under=75 -q
python scripts/check_coverage_gates.py coverage.json
OMNIDESK_RELEASE_SIGNING_KEY="$KEY" scripts/build_release.sh
scripts/docker_scan.sh omnidesk-agent:local
```

Required artifacts:

- `dist/*.whl`
- `dist/*.tar.gz`
- `dist/sbom.json`
- `dist/checksums.txt`
- `dist/*.sig`
- `dist/release_signatures.json`

## 8. Rollback

1. Stop traffic at the reverse proxy or webhook provider.
2. Restore the previous signed wheel/container image.
3. Restore SQLite files from the latest verified backup if schema migration caused corruption.
4. Re-enable traffic and run `scripts/production_smoke_test.py`.
