# OmniDesk Security Baseline

Production defaults are deny-first:

- Admin endpoints require AdminAuth.
- Local admin without token is disabled.
- Webhook signatures are required for enabled channels.
- Plugins require sha256 + a v2 HMAC manifest signature in every environment and run out-of-process (see `SECURITY_SANDBOXING.md`).
- File tools are confined to the workspace with `Path.relative_to()`; the vision tool only reads image files inside the workspace.
- Shell command arguments may not leave the workspace or touch `.git`; `git branch` is listing-only and `git pull` only fetches from `origin`.
- Self-upgrade is PR-only and must pass regression/security gates.

Secrets must come from environment variables or a managed secret store. Never commit `.env`, tokens, browser cookies, OAuth tokens, private keys, or runtime SQLite files.
