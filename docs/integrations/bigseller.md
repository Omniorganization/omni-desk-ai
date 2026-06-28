# BigSeller Connector Service

BigSeller Open API / API Integration Service is a private-approval API. Endpoint paths, signature rules, token exchange behavior, and field names must come from BigSeller's official private documentation. This connector ships with mock mode enabled by default and now supports a configurable real HTTP adapter that remains fail-closed unless the approved endpoint contract is supplied.

## What Is Implemented

- BigSeller credential and runtime configuration from environment variables.
- Access-token state, auth-code exchange, and refresh flow hooks.
- `BigSellerClient` abstraction with deterministic `MockBigSellerAdapter` and configurable `HttpBigSellerClient`.
- Environment-supplied real endpoint paths for orders, inventory, products, fulfillment, auth exchange, and token refresh.
- HMAC-SHA256 request signing scaffold for approved environments, with real mode fail-closed until official canonical signing behavior is configured.
- Real-mode outbound URL hardening: HTTPS-only base URL, explicit official host allowlist, and rejection of localhost/private/link-local literal hosts.
- Order, inventory, product/SKU mapping, and fulfillment sync services.
- Webhook receiver with polling/sync fallback behavior.
- Durable idempotency guard keyed by external ID, store ID, and action type.
- Durable retry/dead-letter queue with SQLite and PostgreSQL backends.
- Webhook replay protection using HMAC, timestamp drift, event ID dedupe, TTL purge, and body-size enforcement.
- Connector-level observability counters in `/integrations/bigseller/sync/status`.
- Admin API routes under `/integrations/bigseller`, including dead-letter list, retry, and resolve operations.
- Audit logging for sync start/end and per-entity outcomes.
- Offline unit and contract tests.

## Configuration

Use `.env.example` as the template and keep real credentials only in the production secret store:

```env
BIGSELLER_ENABLED=false
BIGSELLER_REGISTER_ROUTES=false
BIGSELLER_BASE_URL=
BIGSELLER_ALLOWED_HOSTS=
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

# Required in real mode after official BigSeller API approval.
BIGSELLER_AUTH_CODE_EXCHANGE_PATH=
BIGSELLER_REFRESH_TOKEN_PATH=
BIGSELLER_ORDERS_LIST_PATH=
BIGSELLER_ORDER_DETAIL_PATH=
BIGSELLER_INVENTORY_LIST_PATH=
BIGSELLER_INVENTORY_UPDATE_PATH=
BIGSELLER_PRODUCTS_LIST_PATH=
BIGSELLER_PRODUCT_DETAIL_PATH=
BIGSELLER_FULFILLMENT_SYNC_PATH=

# Generic signing scaffold. Real mode requires this to be true only after
# canonicalization and header names match official docs.
BIGSELLER_REQUEST_SIGNING_ENABLED=false
BIGSELLER_SIGNATURE_HEADER=x-bigseller-signature
BIGSELLER_SIGNATURE_TIMESTAMP_HEADER=x-bigseller-timestamp
BIGSELLER_SIGNATURE_APP_ID_HEADER=x-bigseller-app-id

# Optional response root-key mapping, for example:
# {"orders":"data","inventory":"rows","products":"records"}
BIGSELLER_RESPONSE_ROOT_KEYS=
```

`BIGSELLER_ENABLED=false` disables sync side effects. BIGSELLER_ENABLED does not register routes. `BIGSELLER_REGISTER_ROUTES=false` keeps BigSeller endpoints out of the default gateway attack surface. Routes are registered only when `BIGSELLER_REGISTER_ROUTES=true`.

`BIGSELLER_STATE_BACKEND` accepts:

- `memory`: mock and unit-test only. It is rejected in real mode.
- `sqlite`: restart-safe local state for single-node staging.
- `postgres`: required for horizontally scaled production. Requires `BIGSELLER_POSTGRES_DSN`.

`BIGSELLER_WEBHOOK_MAX_BODY_BYTES` limits webhook payload size. Requests above the limit return HTTP 413 before parsing or signature work. The default is 256 KiB.

## Real Adapter Contract

Real mode now fails closed unless the following are all configured:

1. `BIGSELLER_BASE_URL`, `BIGSELLER_APP_ID`, `BIGSELLER_APP_KEY`, and `BIGSELLER_WEBHOOK_SECRET`.
2. `BIGSELLER_BASE_URL` uses `https://`, uses the approved BigSeller API host, and that host is listed in `BIGSELLER_ALLOWED_HOSTS`.
3. `BIGSELLER_BASE_URL` is not localhost, loopback, private, link-local, reserved, multicast, or unspecified when a literal host is supplied.
4. `BIGSELLER_REQUEST_SIGNING_ENABLED=true` after the canonical request string and header names have been updated from BigSeller private docs.
5. `BIGSELLER_ACCESS_TOKEN` or `BIGSELLER_AUTH_CODE`.
6. Every real sync endpoint path: orders list/detail, inventory list/update, products list/detail, and fulfillment sync.
7. `BIGSELLER_AUTH_CODE_EXCHANGE_PATH` when `BIGSELLER_AUTH_CODE` is provided.
8. `BIGSELLER_REFRESH_TOKEN_PATH` when `BIGSELLER_REFRESH_TOKEN` is provided.
9. `BIGSELLER_STATE_BACKEND=postgres` for horizontally scaled production.
10. Official BigSeller field mappings validated through sanitized contract fixtures.

Endpoint paths may include these placeholders where official docs require path parameters:

```text
{store_id}
{external_order_id} or {order_id}
{external_product_id} or {product_id}
{external_sku} or {sku}
```

The configurable adapter maps common response names such as `data`, `items`, `rows`, `records`, `list`, `order_id`, `shop_id`, `sku`, and `stock`; however, production acceptance must be based on sanitized fixtures from BigSeller's official private documentation, not on assumptions.

## Admin API

- `GET /integrations/bigseller/health`
- `POST /integrations/bigseller/sync/orders`
- `POST /integrations/bigseller/sync/inventory`
- `POST /integrations/bigseller/sync/fulfillment`
- `GET /integrations/bigseller/sync/status`
- `GET /integrations/bigseller/errors`
- `POST /integrations/bigseller/errors/{error_id}/retry`
- `POST /integrations/bigseller/errors/{error_id}/resolve`
- `POST /integrations/bigseller/webhook`

The route registration wires OmniDesk admin auth when `BIGSELLER_REGISTER_ROUTES=true` is explicitly set. Viewer role can inspect health/status/errors. Operator role is required for sync, retry, and resolve side-effect operations. The webhook receiver verifies `BIGSELLER_WEBHOOK_SECRET` when configured. In real mode, missing webhook secret fails closed.

## Mock Mode

Mock mode returns deterministic data:

- Orders `BS-ORDER-1001` and `BS-ORDER-1002`.
- SKUs `SKU-BETTY-001` and `SKU-YMWM127`.
- Inventory rows for `MY-STORE-1`.
- Fulfillment responses that accept status updates without network access.

Repeated sync calls are safe. Completed idempotency keys skip duplicate processing for the same external ID, store ID, and action type.

## Switching To The Real Adapter

After BigSeller approves API access and provides private docs:

1. Configure the official endpoint paths through `BIGSELLER_*_PATH` variables.
2. Configure `BIGSELLER_RESPONSE_ROOT_KEYS` if BigSeller wraps arrays under custom keys.
3. Set `BIGSELLER_ALLOWED_HOSTS` to the official BigSeller API host(s) from the private docs.
4. Enable `BIGSELLER_REQUEST_SIGNING_ENABLED=true` only after confirming the exact canonical signing string and header names.
5. Update webhook signature header parsing if BigSeller's private docs specify different names or payload canonicalization.
6. Add sanitized contract fixtures from the private docs without committing raw credentials, app keys, access tokens, refresh tokens, customer PII, or order payloads.
7. Run mock-mode tests, real-mode contract tests, and one approved staging live smoke.

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
- recent audit events and recent retry/dead-letter errors
- endpoint contract readiness and signing mode in redacted config

Production deployments should export these counters into the shared OmniDesk metrics pipeline after private API behavior is verified.

Recommended production metrics:

```text
bigseller_sync_orders_total
bigseller_sync_inventory_total
bigseller_sync_fulfillment_total
bigseller_webhook_received_total
bigseller_webhook_rejected_total
bigseller_webhook_duplicate_total
bigseller_dead_letter_current
bigseller_sync_duration_ms{operation="orders|inventory|fulfillment"}
```

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
- Request signing and explicit official API host allowlisting are mandatory in real mode.
- `BIGSELLER_STATE_BACKEND=memory` is rejected in real mode.
- Oversized webhook bodies are rejected with 413.
- Real-mode endpoint paths and signing must come from approved BigSeller documentation.
- This connector does not claim production readiness for live BigSeller traffic until the private API contract and live smoke evidence are verified.

## Known Limitations

- The generic signing scaffold may not match BigSeller's final canonical string; official docs remain authoritative.
- SQLite is restart-safe but not a horizontally scalable production state backend.
- Polling cadence is represented by worker configuration, but a scheduler should be wired by the deployment runtime.
- Live smoke evidence is not generated by source tests because it requires private BigSeller credentials and approved endpoints.

## Production Checklist

- BigSeller private API approval completed.
- Official endpoint paths, signing rules, token exchange, token refresh, and field mappings configured.
- Real-mode contract tests added from sanitized official fixtures.
- Webhook signature verification updated to match official docs.
- PostgreSQL state backend configured for multi-instance runtime.
- BigSeller live smoke evidence attached under `release/external-evidence/integrations/`.
- Audit log path included in backup and retention policy.
- No raw token, refresh token, app key, auth code, PII, or order payloads in logs, tests, docs, or committed files.
- Rate-limit values reviewed against BigSeller's approved quota.
