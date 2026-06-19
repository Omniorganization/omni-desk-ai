# Operations Guide

Minimum production checklist:

1. Set `OMNIDESK_ADMIN_TOKEN` and channel webhook secrets.
2. Keep server bound to `127.0.0.1` unless behind a reverse proxy with TLS.
3. Use `/health` for public liveness only.
4. Use `/admin/status` and `/admin/metrics` only with AdminAuth.
5. Back up `~/.omnidesk/*.sqlite3` on a schedule.
6. Rotate webhook secrets and admin tokens after personnel or infrastructure changes.
7. Review self-upgrade proposals as PRs; do not patch `main` directly.
