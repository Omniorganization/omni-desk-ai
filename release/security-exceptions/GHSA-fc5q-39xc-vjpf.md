# Security Exception: GHSA-fc5q-39xc-vjpf

Exception ID: GHSA-fc5q-39xc-vjpf
Status: active
Owner: @Omniorganization/omnidesk-maintainers
Scope: npm apps/desktop-tauri (picomatch via vite -> tinyglobby -> fdir, build and dev server only)
Expires At: 2027-02-01
Upstream Tracking: https://github.com/micromatch/picomatch/pull/254 (fix proposed, not merged or released as of 2026-11-18)
Compensating Controls: the desktop audit still blocks every other high or critical advisory; the exception is limited to this project and expires within 90 days

## Impact

picomatch through 4.0.7 (CVE-2026-112366, CVSS 7.5) can backtrack exponentially on
single-branch repeated extglob patterns such as `+(*)`, blocking the Node.js event loop.
The installed version is 4.0.7, which is also the latest published release, so no
upgrade removes it. `npm audit fix --force` would downgrade vite from 8.3.8 to 6.2.7,
two major versions, which conflicts with `@vitejs/plugin-react` 6.

## Runtime reachability

picomatch is loaded only by vite's file globbing (tinyglobby, fdir) while building the
desktop frontend or serving it in development. The glob patterns come from the vite
configuration and the repository source tree, not from users or network input. vite and
its dependencies are not part of the built frontend bundle or the Tauri application, so
the shipped desktop app does not contain the vulnerable code.

## Compensating control

- `scripts/check_npm_audit.py` accepts only this GHSA, only for `apps/desktop-tauri`,
  and only while this record is valid; any other high or critical advisory still fails
  the Security workflow.
- The web admin npm audit, pip-audit, cargo audit and osv-scanner gates are unchanged.
- The Security Exception Renewal Gate fails 30 days before expiry, and Security Core
  fails once the record has expired.

## Removal criteria

Remove this record in the same PR that updates `apps/desktop-tauri/package-lock.json`
to a picomatch release that fixes GHSA-fc5q-39xc-vjpf, or as soon as the advisory lists a
first patched version that the lockfile can use, whichever comes first.
