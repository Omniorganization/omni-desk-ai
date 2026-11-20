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

## Secrets in Records, API Abuse Limits and Webhook Checks

What the code enforces today:

- **Audit log** (`PermissionManager._redact`):
  - secret-named keys are dropped;
  - self-identifying credentials inside strings are redacted: private keys, GitHub, Slack, AWS, Stripe and `sk-` keys, Google keys and OAuth tokens, JWTs, URL passwords and secret query values, long bearer tokens;
  - content bound by a digest (`x` next to `x_sha256`: typed text, file bodies, messages, plugin arguments) is withheld; the digest and the approval record identify it.
- **Approval records** and the run's copy of a pending proposal keep the content the approver judges, with those credentials redacted. Identities such as `token:OMNIDESK_OWNER_TOKEN` are never redacted, because they drive the requester-cannot-approve rule. Execution binding is unaffected: it uses the scope hash computed over the unredacted arguments.
- **API resource guard:**
  - `/health` and `/ready` are never rate limited and take no concurrency slot, so liveness keeps answering under load or while the rate-limit store is down;
  - every other route fails closed when the store fails;
  - the PostgreSQL limiter keeps one connection per worker thread, reconnects once, prunes expired windows every 5 minutes, and runs off the event loop.
- **`omnidesk validate-webhook-signatures`** runs the live `WebhookGuard` path for every enabled inbound channel. It exits 1 in any of these cases:
  - signatures are not enforced;
  - a configured secret variable is unset;
  - an unsigned delivery, or one signed with a wrong secret, would be accepted.
  Accepting a correctly signed delivery needs each provider's key and is covered by the channel tests.
- Known limits:
  - Credentials in a format the patterns do not know (a typed password, say) stay visible in the approval record, where the approver needs the content.
  - Duplicate webhook deliveries are still answered with 403. A delivery marked as seen and then failing to enqueue is lost. The fix needs the PostgreSQL store and the job queue owners.

## Reporting

Do not include secrets, credentials, private keys, tokens, customer data, or production evidence payloads in public issues. Use a private security channel controlled by the repository owner.
