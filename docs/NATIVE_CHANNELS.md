# Native messaging channel adapters

OmniDesk 0.7.36/0.7.39 promotes the requested OpenClaw-aligned messaging surfaces from catalog-only references to configurable native adapters while preserving OmniDesk's security boundary.

## Native adapter set

| Channel | Adapter key | Inbound route | Outbound target | Required controls |
|---|---|---|---|---|
| WhatsApp Business Cloud | `whatsapp_cloud` / alias `whatsapp` | `/webhooks/whatsapp` | Graph Cloud API phone number id | App-secret HMAC, sender allowlist, approval, audit |
| Telegram | `telegram` | `/webhooks/telegram` | Bot API chat id | Secret token, sender allowlist, approval, audit |
| Slack | `slack` | `/webhooks/slack` | Slack channel id | Slack signing secret, sender/channel allowlist, approval, audit |
| Discord | `discord` | `/webhooks/discord` | Discord channel id | Ed25519 or signed edge bridge, sender/channel allowlist, approval, audit |
| Google Chat | `google_chat` | `/webhooks/google-chat` | Incoming webhook URL (configured, or an allowlisted space) | Google-signed bearer JWT (required in production), channel-token header or HMAC outside production, user/space allowlist, approval, audit |
| Signal | `signal` | `/webhooks/signal` | signal-cli REST bridge recipient | Signed bridge, sender allowlist, approval, audit |
| iMessage | `imessage` | `/webhooks/imessage` | local macOS relay recipient | Signed local relay, foreground confirmation, approval, audit |
| Microsoft Teams | `microsoft_teams` / alias `teams` | `/webhooks/teams` | Bot Framework conversation id | Bearer/HMAC verification, allowlists, approval, audit |
| Matrix | `matrix` | `/webhooks/matrix` | Matrix room id | Hook token in a header or HMAC, room/user allowlist, approval, audit |
| LINE | `line` | `/webhooks/line` | LINE user/group/room id | Channel-secret signature, allowlist, approval, audit |
| WeChat Official | `wechat_official` / alias `wechat` | `/webhooks/wechat` | Official Account openid | Token signature, openid allowlist, passive reply, approval, audit |
| QQ Bot | `qq` | `/webhooks/qq` | `group:`, `channel:`, or `c2c:` recipient | Signed webhook, source/user allowlist, approval, audit |

## Security model

The adapters do not grant automatic execution rights. `capabilities.channels.enabled` must be true before the send tool is registered, each channel must be explicitly enabled, and external sends still pass through the existing `channels.send_text` high-risk approval gate. Webhook routes keep provider signatures or signed bridge HMACs, replay protection, source allowlists, job-queue ingestion, outbound idempotency headers, metrics, and audit records.

## Bridge-backed channels

Signal and iMessage require a trusted local or private bridge because they do not expose the same kind of cloud bot API as Slack or Telegram. These bridges must sign inbound webhooks to OmniDesk and should be deployed on a dedicated OS user/host with minimum privileges.

## Inbound authentication without URL tokens

Credentials in a URL end up in access logs, proxy logs and monitoring, so hook tokens are read from headers only.

### Google Chat

Configure the Chat app's HTTP endpoint with authentication audience **Project number** and set `channels.google_chat.project_number` to that number. Every request then carries `Authorization: Bearer <JWT>` signed by `chat@system.gserviceaccount.com`; OmniDesk verifies the RS256 signature, `iss`, `aud` (the project number) and `exp`/`iat`/`nbf` with 60 seconds of leeway.

- In production the JWT is required by default. `require_google_jwt: false` is only for deployments where a signed edge bridge verifies Google's token itself and forwards the request with the OmniDesk HMAC header; in production that HMAC is then the only accepted method, because the static `x-goog-channel-token` does not cover the body.
- Outside production a bearer token is verified whenever `project_number` is set, and an invalid one is refused rather than downgraded to the other methods. The channel-token header also works there.
- The JWT authenticates the caller, not the body, and its audience is only the project number. Use a Cloud project dedicated to this Chat app, and do not configure `project_number` on a gateway that is meant to accept only a signed bridge.
- Signing certificates come from `https://www.googleapis.com/service_accounts/v1/metadata/x509/chat@system.gserviceaccount.com`, so the gateway needs DNS and HTTPS egress to `www.googleapis.com`. The reference Kubernetes NetworkPolicies do not allow that egress; until the owner adds it, every Google Chat request is refused. Fetching never happens on the request path: the adapter warms the cache in a background thread at startup, refreshes it in the background when it expires (cache lifetime is the response's `max-age`, at most one hour) or when an unknown key id arrives (at most once a minute), and serves the previous certificates for up to six hours if Google cannot be reached. With nothing cached, the request is refused, so the first requests right after startup can be refused until the warm-up finishes.
- The static `?token=` query parameter is refused. `allow_query_token: true` restores it for local development only; it is refused in production regardless of the setting.

### Matrix

Send the hook token as `Authorization: Bearer <MATRIX_WEBHOOK_TOKEN>` (or the `x-matrix-hook-token` header). `?token=` and `?access_token=` are refused unless `allow_query_token: true` is set outside production.

### Access logs

`omnidesk serve` installs a uvicorn logging filter that replaces the value of credential-like query parameters (names containing `token`, `secret`, `passw`, `signature`, `credential`, `session`, `api_key`, and `key`, `code`, `auth*`) with `[REDACTED]` on HTTP access lines and WebSocket accept/close lines. Reverse proxies in front of the gateway log the query string by default (nginx `$request`); log `$uri` for `/webhooks/` and `/app/ws` instead.

## Google Chat outbound spaces

When `GOOGLE_CHAT_WEBHOOK_URL` is set every send goes to that webhook and the recipient is ignored. Without it, the recipient must be an incoming-webhook URL of the form `https://chat.googleapis.com/v1/spaces/<id>/messages?...` whose space is listed in `channels.google_chat.allowed_outbound_spaces` (`spaces/<id>` or `<id>`); any other space, host, port or path is refused before a request is made.

## X CRC challenge

`GET /webhooks/x` answers X's challenge-response check with HMAC-SHA256 of `crc_token` keyed by `X_WEBHOOK_CRC_TOKEN` (the app's consumer secret, as X requires). The endpoint is unauthenticated, so:

- it only answers when the X channel is enabled, and only for `crc_token` values of 1–128 characters from the base64/base64url alphabet; anything else (JSON, whitespace, long strings) gets 400 instead of a signature;
- `X_WEBHOOK_SECRET` (the OmniDesk bridge HMAC that signs `POST /webhooks/x`) must be a different secret. With the same value the CRC responder would sign data with the webhook key, so the CRC endpoint returns 503 and a production runtime refuses to start.

## Migration

1. Google Chat: switch the app's authentication audience to *Project number*, set `channels.google_chat.project_number` (quoted or unquoted), and remove `?token=` from the endpoint URL; a request that still carries `?token=` is refused even with a valid JWT. Allow DNS and egress to `www.googleapis.com:443`. Signed-bridge deployments that cannot forward the JWT set `require_google_jwt: false` explicitly and must sign every request with the bridge HMAC.
2. Google Chat outbound without `GOOGLE_CHAT_WEBHOOK_URL`: list the target spaces in `allowed_outbound_spaces`.
3. Matrix: move the hook token from the URL to `Authorization: Bearer`.
4. X: make sure `X_WEBHOOK_CRC_TOKEN` (consumer secret) and `X_WEBHOOK_SECRET` (bridge HMAC) differ.
5. Production with Google Chat, Matrix or X enabled requires `gateway.require_webhook_signatures: true`; `omnidesk production-check` lists all of the above.

Rollback: the previous release rejects unknown configuration keys, so remove `project_number`, `require_google_jwt`, `allow_query_token` and `allowed_outbound_spaces` from `channels.google_chat` and `allow_query_token` from `channels.matrix` before deploying it.
