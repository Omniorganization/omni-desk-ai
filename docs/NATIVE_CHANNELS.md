# Native messaging channel adapters

OmniDesk 0.7.36/0.7.39 promotes the requested OpenClaw-aligned messaging surfaces from catalog-only references to configurable native adapters while preserving OmniDesk's security boundary.

## Native adapter set

| Channel | Adapter key | Inbound route | Outbound target | Required controls |
|---|---|---|---|---|
| WhatsApp Business Cloud | `whatsapp_cloud` / alias `whatsapp` | `/webhooks/whatsapp` | Graph Cloud API phone number id | App-secret HMAC, sender allowlist, approval, audit |
| Telegram | `telegram` | `/webhooks/telegram` | Bot API chat id | Secret token, sender allowlist, approval, audit |
| Slack | `slack` | `/webhooks/slack` | Slack channel id | Slack signing secret, sender/channel allowlist, approval, audit |
| Discord | `discord` | `/webhooks/discord` | Discord channel id | Ed25519 or signed edge bridge, sender/channel allowlist, approval, audit |
| Google Chat | `google_chat` | `/webhooks/google-chat` | Incoming webhook URL | Channel token or HMAC, user/space allowlist, approval, audit |
| Signal | `signal` | `/webhooks/signal` | signal-cli REST bridge recipient | Signed bridge, sender allowlist, approval, audit |
| iMessage | `imessage` | `/webhooks/imessage` | local macOS relay recipient | Signed local relay, foreground confirmation, approval, audit |
| Microsoft Teams | `microsoft_teams` / alias `teams` | `/webhooks/teams` | Bot Framework conversation id | Bearer/HMAC verification, allowlists, approval, audit |
| Matrix | `matrix` | `/webhooks/matrix` | Matrix room id | Hook token or HMAC, room/user allowlist, approval, audit |
| LINE | `line` | `/webhooks/line` | LINE user/group/room id | Channel-secret signature, allowlist, approval, audit |
| WeChat Official | `wechat_official` / alias `wechat` | `/webhooks/wechat` | Official Account openid | Token signature, openid allowlist, passive reply, approval, audit |
| QQ Bot | `qq` | `/webhooks/qq` | `group:`, `channel:`, or `c2c:` recipient | Signed webhook, source/user allowlist, approval, audit |

## Security model

The adapters do not grant automatic execution rights. `capabilities.channels.enabled` must be true before the send tool is registered, each channel must be explicitly enabled, and external sends still pass through the existing `channels.send_text` high-risk approval gate. Webhook routes keep provider signatures or signed bridge HMACs, replay protection, source allowlists, job-queue ingestion, outbound idempotency headers, metrics, and audit records.

## Bridge-backed channels

Signal and iMessage require a trusted local or private bridge because they do not expose the same kind of cloud bot API as Slack or Telegram. These bridges must sign inbound webhooks to OmniDesk and should be deployed on a dedicated OS user/host with minimum privileges.
