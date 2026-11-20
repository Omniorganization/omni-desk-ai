# Security Policy

## Supported Status

Current status: `1.12.7+root-monorepo-production-ga-candidate`.

This is a source-gated production GA candidate. It must not be represented as customer-distribution Production GA until external evidence gates pass against real systems.

## Required Controls

- Do not weaken approval, dual approval, sandbox, RBAC, CSP, webhook signatures, device signatures, audit logs, or external GA evidence gates.
- L2 and higher write/action capabilities require approval gating.
- L3 and L4 actions require human approval, auditability, timeout/revocation behavior, and rollback or containment evidence.
- Signed device requests are required for sensitive Desktop and Mobile runtime actions.
- Release artifacts must include portable checksums and release manifests.
- Customer-distribution releases require signed artifacts, SBOM/provenance, image digests, and real external evidence.

## Webhook Replay and Redelivery

What the code enforces today (`security/webhook_security.py`, `server_routes/webhook_guard.py`):

- **Order of checks.** A delivery is checked in this order:
  1. the provider-native signature;
  2. the signed nonce, which is claimed immediately so a captured signed query cannot carry another body;
  3. the event timestamp;
  4. the per-source rate limit;
  5. the replay digest.
- **Digests.** A digest is recorded only after the delivery is durably enqueued. A failed enqueue therefore leaves the provider's redelivery acceptable. The job queue's deduplication covers two copies that arrive at the same time.
- **Duplicates.** A verified duplicate of an enqueued delivery is answered `200 {"ok": true, "duplicate": true}` and is not processed again. If the provider expects a specific body (QQ's op 13 answer or op 12 ACK), the adapter's `webhook_response(None, payload)` rebuilds it from the verified payload, and a refusal there is a 403. An unsigned or wrongly signed duplicate is still refused with 403.
- **Event timestamps.** An event may be at most `replay_ttl_seconds` old (default 7 days, because Meta and WhatsApp redeliver that long with the original event time) and at most 5 minutes in the future. The window never exceeds how long digests are remembered (`seen_retention_seconds`), so every accepted replay is recognised. Providers that sign a request timestamp, such as Slack, keep their own short window in the adapter.
- **Rate limits.** A source over its limit gets `429` with `Retry-After`.
- **Known limit:** a WeChat redelivery that reuses the signed nonce of a request whose enqueue failed is refused, because the nonce was already claimed. Releasing it would reopen the forged-body window.

## Reporting

Do not include secrets, credentials, private keys, tokens, customer data, or production evidence payloads in public issues. Use a private security channel controlled by the repository owner.
