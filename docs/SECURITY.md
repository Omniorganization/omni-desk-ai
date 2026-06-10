# OmniDesk Security Baseline

Production defaults are deny-first:

- Admin endpoints require AdminAuth.
- Local admin without token is disabled.
- Webhook signatures are required for enabled channels.
- Plugins require sha256 + HMAC signature and run out-of-process.
- File tools are confined to the workspace with `Path.relative_to()`.
- Self-upgrade is PR-only and must pass regression/security gates.

Secrets must come from environment variables or a managed secret store. Never commit `.env`, tokens, browser cookies, OAuth tokens, private keys, or runtime SQLite files.
