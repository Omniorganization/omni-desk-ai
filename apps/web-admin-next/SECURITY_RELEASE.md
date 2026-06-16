# Web Admin Security Release Gate

Required before production:

- Serve only over HTTPS behind the enterprise gateway.
- Set owner/operator/viewer tokens as server-side or operator-managed secrets.
- Keep browser session token in memory only; do not persist it to localStorage.
- Verify CSP, X-Frame-Options, Referrer-Policy, and Permissions-Policy headers.
- Run `npm ci && npm run typecheck && npm test && npm run build`.
- Build the container from the digest-pinned `NODE_BASE_IMAGE` in `Dockerfile`; do not replace it with a floating `node:*` tag.
- Run the image as UID/GID `10001:10001` with `--read-only`, `--tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m`, `--cap-drop=ALL`, and `--security-opt no-new-privileges:true`.
- Require the Docker `HEALTHCHECK` to pass before promotion and verify the Web Admin OCI digest is captured in `release_metadata.json`.
