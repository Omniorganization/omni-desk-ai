# BigSeller Connector Service

BigSeller Open API / API Integration Service is a private-approval API. Endpoint paths, signature rules, token exchange behavior, and field names must come from BigSeller's official private documentation. This connector intentionally ships as a production-oriented scaffold with mock mode enabled by default and no unverified BigSeller endpoints hardcoded.

## What Is Implemented

- BigSeller credential and runtime configuration from environment variables.
- Access-token state and refresh flow hooks.
- `BigSellerClient` abstraction with a real HTTP scaffold and deterministic `MockBigSellerAdapter`.
- Order, inventory, product/SKU mapping, and fulfillment sync services.
- Webhook receiver with polling/sync fallback behavior.
- Durable idempotency guard keyed by external ID, store ID, and action type.
- Durable retry/dead-letter queue with SQLite and PostgreSQL backends.
- Webhook replay protection using HMAC, timestamp drift, event ID dedupe, TTL purge, and body-size enforcement.
- Connector-level observability counters in `/integrations/bigseller/sync/status`.
- Audit logging for sync start/end and per-entity outcomes.
- Admin API routes under `/integrations/bigseller`.
- Offline unit and contract tests.

## Configuration

Use `.env.example` as the template:

```env
BIGSELLER_ENABLED=false
BIGSELLER_BASE_URL=
BIGSELLER_APP_ID=
BIGSELLER_APP_KEY=
BIGSELLER_AUTH_CODE=
BIGSELLER_ACCESS_TOKEN=
BIGSELLER_REFRESH_TOKEN=
BIGSELLER_TOKEN_EXPIRES_AT=
BIGSELLER_WEBHOOK_SECRET=
BIGSELLER_SYNC_INTERVAL_SECONDS=300
BIGSELLER_MAX_RETRIES=3
BIGSELLER_RATE_LIMIT_PER_MINUTE=60
BIGSELLER_USE_MOCK=true
BIGSELLER_STATE_BACKEND=sqlite
BIGSELLER_STATE_DB_PATH=
BIGSELLER_POSTGRES_DSN=
BIGSELLER_WEBHOOK_REPLAY_WINDOW_SECONDS=300
BIGSELLER_WEBHOOK_EVENT_TTL_SECONDS=86400
BIGSELLER_WEBHOOK_MAX_BODY_BYTES=262144
```

`BIGSELLER_ENABLED=false` disables sync side effects. To run the scaffold offline, set `BIGSELLER_ENABLED=true` and keep `BIGSELLER_USE_MOCK=true`.

`BIGSELLER_STATE_BACKEND` accepts:

- `memory`: mock and unit-test only. It is rejected in real mode.
- `sqlite`: restart-safe local state for single-node staging.
- `postgres`: required for horizontally scaled production. Requires `BIGSELLER_POSTGRES_DSN`.

`BIGSELLER_WEBHOOK_MAX_BODY_BYTES` limits webhook payload size. Requests above the limit return HTTP 413 before parsing or signature work. The default is 256 KiB.

## Admin API

- `GET /integrations/bigseller/health`
- `POST /integrations/bigseller/sync/orders`
- `POST /integrations/bigseller/sync/inventory`
- `POST /integrations/bigseller/sync/fulfillment`
- `GET /integrations/bigseller/sync/status`
- `POST /integrations/bigseller/webhook`

The route registration wires OmniDesk admin auth when the full gateway app is used. The webhook receiver verifies `BIGSELLER_WEBHOOK_SECRET` when configured. In real mode, missing webhook secret fails closed.

## Mock Mode

Mock mode returns deterministic data:

- Orders `BS-ORDER-1001` and `BS-ORDER-1002`.
- SKUs `SKU-BETTY-001` and `SKU-YMWM127`.
- Inventory rows for `MY-STORE-1`.
- Fulfillment responses that accept status updates without network access.

Repeated sync calls are safe. Completed idempotency keys skip duplicate processing for the same external ID, store ID, and action type.

## Switching To The Real Adapter

After BigSeller approves API access and provides private docs:

1. Replace `HttpBigSellerClient.exchange_auth_code()` and `refresh_access_token()` with the official auth endpoints and response mappings.
2. Replace `list_orders()`, `get_order()`, `list_inventory()`, `update_inventory()`, `list_products()`, `get_product()`, and `sync_fulfillment_status()` with official endpoint paths and field mapping.
3. Implement the official signing algorithm and required headers in `HttpBigSellerClient.request()`.
4. Update webhook signature header parsing if BigSeller's private docs specify different names or payload canonicalization.
5. Add contract fixtures from the private docs without committing raw credentials, app keys, access tokens, or refresh tokens.

Real mode fails closed when `BIGSELLER_BASE_URL`, `BIGSELLER_APP_ID`, `BIGSELLER_APP_KEY`, `BIGSELLER_WEBHOOK_SECRET`, and either `BIGSELLER_ACCESS_TOKEN` or `BIGSELLER_AUTH_CODE` are missing.

## Webhook Replay Protection

Real-mode webhook delivery requires:

- HMAC-SHA256 signature in `x-bigseller-signature-256` or `x-bigseller-signature`.
- Timestamp in `x-bigseller-timestamp`, `x-bigseller-request-timestamp`, or `x-request-timestamp`.
- Timestamp drift within `BIGSELLER_WEBHOOK_REPLAY_WINDOW_SECONDS`.
- Event ID in `event_id`, `webhook_id`, `id`, `x-bigseller-event-id`, or `x-event-id`.
- Body size within `BIGSELLER_WEBHOOK_MAX_BODY_BYTES`.
- Durable event ID dedupe through the configured state backend.
- TTL purge of expired idempotency records through `BIGSELLER_WEBHOOK_EVENT_TTL_SECONDS`.

Duplicate webhook events return `ok=true` with `handled=duplicate` and do not trigger repeated sync side effects.

## Observability

`GET /integrations/bigseller/sync/status` exposes:

- idempotency backend and durability state
- retry/dead-letter queue backend and counts
- connector counters for sync, webhook receive/reject/duplicate, and current dead-letter gauge
- last sync result
- last operation duration snapshot
- recent audit events

Production deployments should export these counters into the shared OmniDesk metrics pipeline after private API behavior is verified.

## Live Smoke Evidence

BigSeller customer-distribution readiness requires a live smoke evidence file:

```text
release/external-evidence/integrations/bigseller-live-smoke.json
```

Minimum fields:

```json
{
  "schema": "omnidesk-bigseller-live-smoke/v1",
  "status": "passed",
  "produced_at": "ISO-8601 timestamp from the live run",
  "producer": "approved BigSeller staging workflow or operator",
  "environment": "staging",
  "store_id": "redacted real store id",
  "auth_success": true,
  "order_list_success": true,
  "inventory_list_success": true,
  "webhook_signature_verified": true,
  "webhook_replay_guard_verified": true,
  "secret_leakage_checked": true,
  "no_secret_leakage": true,
  "trace_id": "distributed trace id",
  "audit_event_id": "audit log event id",
  "p95_latency_ms": 2500,
  "error_rate": 0
}
```

Do not commit raw secrets, app keys, access tokens, refresh tokens, auth codes, customer PII, or order payloads.

## Security Notes

- Raw token, refresh token, app key, authorization, cookie, and secret fields are redacted before queue or error persistence.
- The real adapter does not log request headers or credential values.
- `BIGSELLER_APP_KEY` must remain a secret and must not be committed.
- Webhook verification is mandatory in real mode.
- `BIGSELLER_STATE_BACKEND=memory` is rejected in real mode.
- Oversized webhook bodies are rejected with 413.
- This scaffold does not claim production readiness for live BigSeller traffic until the private API contract is implemented and verified.

## Known Limitations

- Real endpoint paths and signature rules are intentionally not implemented.
- SQLite is restart-safe but not a horizontally scalable production state backend.
- Polling cadence is represented by worker configuration, but a scheduler should be wired by the deployment runtime.
- Live smoke evidence is not generated by source tests because it requires private BigSeller credentials and approved endpoints.

## Production Checklist

- BigSeller private API approval completed.
- Official endpoint paths, signing rules, token exchange, token refresh, and field mappings implemented.
- Real-mode contract tests added from sanitized official fixtures.
- Webhook signature verification updated to match official docs.
- PostgreSQL state backend configured for multi-instance runtime.
- BigSeller live smoke evidence attached under `release/external-evidence/integrations/`.
- Audit log path included in backup and retention policy.
- No raw token, refresh token, app key, auth code, PII, or order payloads in logs, tests, docs, or committed files.
- Rate-limit values reviewed against BigSeller's approved quota.
