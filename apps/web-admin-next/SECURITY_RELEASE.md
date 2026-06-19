# Web Admin Security Release Gate

Required before production:

- Serve only over HTTPS behind the enterprise gateway.
- Set owner/operator/viewer tokens as server-side or operator-managed secrets.
- Keep browser session token in memory only; do not persist it to localStorage.
- Verify CSP, X-Frame-Options, Referrer-Policy, and Permissions-Policy headers.
- Run `npm ci && npm run typecheck && npm test && npm run build`.
