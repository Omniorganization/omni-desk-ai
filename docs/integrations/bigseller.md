# BigSeller Connector Service

BigSeller Open API / API Integration Service is a private-approval API. Endpoint paths, signature rules, token exchange behavior, and field names must come from BigSeller's official private documentation. This connector intentionally ships as a production-oriented scaffold with mock mode enabled by default and no unverified BigSeller endpoints hardcoded.

## What Is Implemented

- BigSeller credential and runtime configuration from environment variables.
- Access-token state and refresh flow hooks.
- `BigSellerClient` abstraction with a real HTTP scaffold and deterministic `MockBigSellerAdapter`.
- Order, inventory, product/SKU mapping, and fulfillment sync services.
- Webhook receiver with polling/sync fallback behavior.
- Idempotency guard keyed by external ID, store ID, and action type.
- Retryable sync error queue with dead-letter transition after max retries.
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
```

`BIGSELLER_ENABLED=false` disables sync side effects. To run the scaffold offline, set `BIGSELLER_ENABLED=true` and keep `BIGSELLER_USE_MOCK=true`.

## Admin API

- `GET /integrations/bigseller/health`
- `POST /integrations/bigseller/sync/orders`
- `POST /integrations/bigseller/sync/inventory`
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

Real mode fails closed when `BIGSELLER_BASE_URL`, `BIGSELLER_APP_ID`, `BIGSELLER_APP_KEY`, and either `BIGSELLER_ACCESS_TOKEN` or `BIGSELLER_AUTH_CODE` are missing.

## Security Notes

- Raw token, refresh token, app key, authorization, cookie, and secret fields are redacted before queue or error persistence.
- The real adapter does not log request headers or credential values.
- `BIGSELLER_APP_KEY` must remain a secret and must not be committed.
- Webhook verification is mandatory in real mode.
- This scaffold does not claim production readiness for live BigSeller traffic until the private API contract is implemented and verified.

## Known Limitations

- Real endpoint paths and signature rules are intentionally not implemented.
- The default idempotency guard and sync error queue are process-local scaffolds; deploy a shared durable backend before multi-instance production.
- Polling cadence is represented by worker configuration, but a scheduler should be wired by the deployment runtime.

## Production Checklist

- BigSeller private API approval completed.
- Official endpoint paths, signing rules, token exchange, token refresh, and field mappings implemented.
- Real-mode contract tests added from sanitized official fixtures.
- Webhook signature verification updated to match official docs.
- Shared durable idempotency and error queue backend configured for multi-instance runtime.
- Audit log path included in backup and retention policy.
- No raw token, refresh token, app key, or auth code in logs, tests, docs, or committed files.
- Rate-limit values reviewed against BigSeller's approved quota.
