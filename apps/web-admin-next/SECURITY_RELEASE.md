# Web Admin Security Release Gate

Required before production:

- Serve only over HTTPS behind the enterprise gateway.
- Set owner/operator/viewer tokens as server-side or operator-managed secrets.
- Keep browser session token in memory only; do not persist it to localStorage.
- Verify CSP, X-Frame-Options, Referrer-Policy, and Permissions-Policy headers.
- Set `OMNI_WEB_SESSION_SECRET` (32+ characters, same value on every replica) so session seals survive restarts; without it every restart signs everyone out.
- Put nginx (or another proxy that overwrites `X-Forwarded-For` with the peer address) in front: per-client limits key on its rightmost entry.
- Configure the gateway with `api_resource_guard.trusted_client_ip_header: x-omnidesk-client-ip` and `trusted_client_ip_header_peers` set to this server's address, or its pre-auth limits stay shared by all users.
- Run `npm ci && npm run typecheck && npm test && npm run build`.
