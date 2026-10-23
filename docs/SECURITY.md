# OmniDesk Security Baseline

Production defaults are deny-first:

- Admin endpoints require AdminAuth.
- Local admin without token is disabled.
- Webhook signatures are required for enabled channels.
- Plugins require sha256 + a v2 HMAC manifest signature in every environment and run out-of-process (see `SECURITY_SANDBOXING.md`).
- File tools are confined to the workspace with `Path.relative_to()`; the vision tool only reads image files inside the workspace.
- Shell command arguments may not leave the workspace or touch `.git`; `git branch` is listing-only and `git pull` only fetches from `origin`.
- Self-upgrade is PR-only and must pass regression/security gates.
- An approval request for a side-effecting tool call carries the full content it acts on (message and email bodies, file content, typed text, browser scripts, PR bodies, vision instructions, plugin arguments) with its SHA-256, so the approver sees everything and the scope hash binds every character: a resumed or session-approved call recomputes the hash from the content it is about to act on, and any difference needs a new approval. Proposals never carry a truncated preview; `tests/test_tool_approval_content_binding.py` scans for one. The approval store and audit log therefore hold that content.

Secrets must come from environment variables or a managed secret store. Never commit `.env`, tokens, browser cookies, OAuth tokens, private keys, or runtime SQLite files.
