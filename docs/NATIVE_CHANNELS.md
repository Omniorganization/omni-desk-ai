# Native messaging channel adapters

OmniDesk ships configurable adapters for the messaging surfaces below. This page says, per channel, what is actually verified on inbound traffic and how outbound delivery behaves.

## Native adapter set

"Inbound authentication" is what the gateway verifies on each webhook request. "OmniDesk bridge HMAC" means `x-omnidesk-webhook-signature-256`, which no provider sends: such a channel only accepts traffic re-signed by a relay you deploy yourself (none ships in this repository).

| Channel | Adapter key | Inbound route | Inbound authentication | Outbound recipient |
|---|---|---|---|---|
| WhatsApp Business Cloud | `whatsapp_cloud` / alias `whatsapp` | `/webhooks/whatsapp` | `X-Hub-Signature-256` (app secret) | WhatsApp id (phone number) |
| Telegram | `telegram` | `/webhooks/telegram` | `X-Telegram-Bot-Api-Secret-Token` | chat id |
| Meta Graph (Page / Instagram) | `meta_graph` | `/webhooks/meta` | `X-Hub-Signature-256` (app secret) | PSID / IGSID |
| WeChat Official | `wechat_official` / alias `wechat` | `/webhooks/wechat` | SHA-1 token signature, freshness, single-use nonce (plaintext mode only) | openid |
| LINE | `line` | `/webhooks/line` | `X-Line-Signature` (channel secret) | user / group / room id |
| Slack | `slack` | `/webhooks/slack` | Slack v0 signing secret with timestamp skew | channel id |
| Lark / Feishu | `lark`, `feishu` | `/webhooks/lark`, `/webhooks/feishu` | Verification token (schema 2.0 `header.token`); with an Encrypt Key, AES decryption plus `X-Lark-Signature` | `oc_…` chat, `ou_…` open id, `on_…` union id, or email |
| DingTalk | `dingtalk` | `/webhooks/dingtalk` | `timestamp`/`sign` headers keyed by the app secret (5-minute window, single use) | `group:<openConversationId>` (or a `cid…` id), `user:<userId>`, or `robot` for the custom group robot |
| X | `x` | `/webhooks/x` (+ CRC `GET`) | `X-Twitter-Webhooks-Signature-OAuth2` / `X-Twitter-Webhooks-Signature` | `public` (post) or `dm:<user id>` |
| Google Chat | `google_chat` | `/webhooks/google-chat` | Google-signed bearer JWT (required in production); channel-token header or bridge HMAC outside production | incoming-webhook space (see below) |
| Matrix | `matrix` | `/webhooks/matrix` | Hook token in a header, or bridge HMAC | room id |
| Discord | `discord` | `/webhooks/discord` | Ed25519 interaction signature when PyNaCl is installed (it is not a declared dependency), otherwise bridge HMAC | channel id |
| Microsoft Teams | `microsoft_teams` / alias `teams` | `/webhooks/teams` | Static bearer token or bridge HMAC. Bot Framework JWT validation is not implemented, so Bot Connector traffic is refused | Bot Framework conversation id |
| QQ Bot | `qq` | `/webhooks/qq` | Bridge HMAC only. QQ's Ed25519 signature and op 13 callback validation are not implemented | `group:`, `channel:`, or `c2c:` recipient |
| Signal | `signal` | `/webhooks/signal` | Bridge HMAC (signal-cli has no signed webhook) | signal-cli REST bridge recipient |
| iMessage | `imessage` | `/webhooks/imessage` | Bridge HMAC from a local relay | local macOS relay recipient |

None of these adapters has been exercised against the real provider by a committed live run yet; the parsing, signing and error handling are tested against recorded provider payloads and the providers' documented examples.

## Security model

The adapters do not grant automatic execution rights. `capabilities.channels.enabled` must be true before the send tool is registered, each channel must be explicitly enabled, and external sends still pass through the existing `channels.send_text` high-risk approval gate. Webhook routes keep provider signatures (or signed bridge HMACs where the table says so), replay protection, source allowlists, job-queue ingestion, metrics, and audit records.

The approved recipient is where the message goes: an adapter refuses a recipient it cannot honour instead of sending somewhere else (DingTalk's custom robot can only post to its own group; a configured Google Chat webhook only to its own space).

## Delivery semantics

A send that the provider may have performed must not be sent again blindly, because most providers cannot deduplicate it. The shared transport (`channels/http_client.py`) therefore repeats a request only when that cannot duplicate a message:

- the request never reached the provider (connection refused, DNS, connect or pool timeout, proxy failure);
- the provider answered 408, 425 or 429, meaning it did not act; a provider back-off hint (`Retry-After`, Telegram `parameters.retry_after`, Discord `retry_after`, Matrix `retry_after_ms`) longer than 3 seconds is handed to the outbox instead of being slept through;
- the request is idempotent: `GET`/`PUT`, or a send carrying a dedupe key the provider honours.

After a read timeout, a dropped connection or a 5xx, the error goes straight to the outbound dispatcher, which records a provider without native dedupe as `ambiguous` for reconciliation (`scripts/reconcile_outbound.py` lists them; requeue only after checking the provider) rather than resending it.

| Provider dedupe | Channels | Effect |
|---|---|---|
| Native | LINE (`X-Line-Retry-Key`, a UUID derived from the outbox key, 24 h), Matrix (`PUT …/send/m.room.message/{txnId}`), Lark / Feishu (`uuid` body field, 1 h) | the transport and the outbox may retry; LINE's `409` with `x-line-accepted-request-id` is recorded as sent |
| Transport only | Discord (`nonce` + `enforce_nonce`, a few minutes) | the transport may retry; the outbox treats failures as ambiguous |
| None | Telegram, WhatsApp, Meta Graph, WeChat, DingTalk, X, Slack, Google Chat, Teams, QQ, Signal, iMessage | timeouts and 5xx become `ambiguous` |

No custom idempotency header is sent to public providers, since none honours one; Signal and iMessage requests carry `X-Omnidesk-Client-Request-Id` for OmniDesk's own bridges.

Errors are classified from what the provider says, not only the HTTP status: WeChat `errcode`, DingTalk `errcode`/`code`, Lark `code` and Slack `ok:false` inside HTTP 200 are failures (for example WeChat 45015, outside the 48-hour window, dead-letters as `policy_error` instead of being recorded as sent); Graph throttling codes (4, 17, 32, 613, 130429, 131056, …) retry as `rate_limit` even in a 400; a blocked or deactivated Telegram recipient dead-letters as `recipient_unavailable` without an auth alert. WeChat and Lark refresh a rejected access token once and resend, because the provider refused the first attempt. Successful sends record the provider message id (Telegram `message_id`, WhatsApp `wamid`, LINE `sentMessages[].id`, Lark `message_id`, DingTalk `processQueryKey`, X post or DM event id).

"Sent" means the provider accepted the message. WhatsApp reports later delivery failures (for example 131047, outside the 24-hour window) only through status webhooks, which are not reconciled into the outbox yet.

## Provider notes

### DingTalk

Inbound robot callbacks are verified with DingTalk's `timestamp` and `sign` headers: `sign = base64(HMAC-SHA256(app secret, timestamp + "\n" + app secret))`, with the timestamp within 5 minutes. The signature does not cover the body, so each one is accepted once per process; `DINGTALK_APP_SECRET` must be set. A deployment that fronts DingTalk with a body-signing relay can still use the bridge HMAC (`DINGTALK_WEBHOOK_SECRET`) when the DingTalk headers are absent.

Outbound uses the robot OpenAPI with an access token from `DINGTALK_APP_KEY`/`DINGTALK_APP_SECRET` (robot code defaults to the app key): `group:<openConversationId>` (or a bare `cid…` id) for a group and `user:<userId>` for a 1:1 chat. Inbound 1:1 messages are threaded as `user:<senderStaffId>` so a reply reaches the sender. `robot` posts through the custom group robot webhook (`DINGTALK_ROBOT_TOKEN`, optional `DINGTALK_ROBOT_SECRET`), which can only reach its own group; any other recipient is refused.

### Lark / Feishu

Schema 2.0 events carry the verification token in `header.token`; legacy 1.0 events at the top level. When `LARK_ENCRYPT_KEY`/`FEISHU_ENCRYPT_KEY` is set, every body must be `{"encrypt": …}` (AES-256-CBC, key SHA-256 of the Encrypt Key, IV in the first 16 bytes) and every event push must carry a valid `X-Lark-Signature` (`sha256(timestamp + nonce + Encrypt Key + body)`); only the URL-verification challenge is unsigned. Message `content` is parsed from its JSON string, mention placeholders (`@_user_1`) are removed, and messages from apps are ignored. Outbound `content` is sent as a JSON string with a `uuid`, and the tenant access token is cached until shortly before it expires.

### X

`X_WEBHOOK_CRC_TOKEN` holds the secret X signs with: the OAuth 2.0 client secret for `X-Twitter-Webhooks-Signature-OAuth2`, or the OAuth 1.0a consumer secret for the legacy `X-Twitter-Webhooks-Signature`. Event bodies must be JSON objects. Posts (`POST /2/tweets`, recipient `public`) and DMs (`POST /2/dm_conversations/with/{id}/messages`, recipient `dm:<user id>`) are signed with OAuth 1.0a user context from `X_API_KEY`/`X_API_SECRET`/`X_ACCESS_TOKEN`/`X_ACCESS_SECRET`. `X_BEARER_TOKEN` is used only when those are missing and only works if it holds an OAuth 2.0 user-context token; an app-only bearer token cannot post, and nothing refreshes a user token. Inbound DMs are threaded as `dm:<sender id>`, and echoes of the account's own DMs are ignored.

### WeChat Official

Access tokens come from `cgi-bin/stable_token` in normal mode, so several replicas share one token instead of invalidating each other, and the app secret travels in the request body. Only plaintext message mode is implemented.

### Meta Graph

The page access token is sent as `Authorization: Bearer`, not `?access_token=`, so it does not land in proxy logs.

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

When `GOOGLE_CHAT_WEBHOOK_URL` is set every send goes to that webhook, so the recipient must name the webhook's space (`spaces/<id>`, or an incoming-webhook URL for that space); any other recipient is refused rather than silently redirected. Without it, the recipient must be an incoming-webhook URL of the form `https://chat.googleapis.com/v1/spaces/<id>/messages?...` whose space is listed in `channels.google_chat.allowed_outbound_spaces` (`spaces/<id>` or `<id>`); any other space, host, port or path is refused before a request is made.

## X CRC challenge

`GET /webhooks/x` answers X's challenge-response check with `sha256=` + base64 HMAC-SHA256 of `crc_token` keyed by `X_WEBHOOK_CRC_TOKEN`, the same secret X signs events with. The endpoint is unauthenticated, so:

- it only answers when the X channel is enabled, and only for `crc_token` values of 1–128 characters from the base64/base64url alphabet; anything else (JSON, whitespace, long strings) gets 400 instead of a signature. Event bodies must be JSON objects, so a CRC answer can never be replayed as an event signature;
- `X_WEBHOOK_SECRET` (the optional OmniDesk bridge HMAC for relayed `POST /webhooks/x`) must be a different secret. With the same value the CRC responder would sign data with the bridge key, so the CRC endpoint returns 503 and a production runtime refuses to start.

## Migration

1. Google Chat: switch the app's authentication audience to *Project number*, set `channels.google_chat.project_number` (quoted or unquoted), and remove `?token=` from the endpoint URL; a request that still carries `?token=` is refused even with a valid JWT. Allow DNS and egress to `www.googleapis.com:443`. Signed-bridge deployments that cannot forward the JWT set `require_google_jwt: false` explicitly and must sign every request with the bridge HMAC.
2. Google Chat outbound without `GOOGLE_CHAT_WEBHOOK_URL`: list the target spaces in `allowed_outbound_spaces`. With it, use `spaces/<id>` of that webhook as the recipient.
3. Matrix: move the hook token from the URL to `Authorization: Bearer`.
4. X: make sure `X_WEBHOOK_CRC_TOKEN` (the secret X signs with) and `X_WEBHOOK_SECRET` (bridge HMAC, optional) differ. Set the four OAuth 1.0a user credentials to post; use recipient `public` for posts and `dm:<user id>` for DMs.
5. DingTalk: set `DINGTALK_APP_KEY`/`DINGTALK_APP_SECRET` for callbacks and replies; recipients become `group:…`, `cid…`, `user:…` or `robot`.
6. Lark / Feishu: if the app has an Encrypt Key, set `LARK_ENCRYPT_KEY`/`FEISHU_ENCRYPT_KEY`; plaintext events are then refused.
7. Production with Google Chat, Matrix or X enabled requires `gateway.require_webhook_signatures: true`; `omnidesk production-check` lists all of the above.

Rollback: the previous release rejects unknown configuration keys, so remove `project_number`, `require_google_jwt`, `allow_query_token` and `allowed_outbound_spaces` from `channels.google_chat` and `allow_query_token` from `channels.matrix` before deploying it. Rolling back the delivery-semantics change restores transport-level resends of timed-out sends (duplicate messages) and HTTP-200 provider errors recorded as sent; rows marked `ambiguous` or dead-lettered meanwhile stay as they are.
