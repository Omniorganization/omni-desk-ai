# Production Boundary Fixes

This patch fixes four production-readiness gaps:

1. Config loading no longer recurses in `_safe_yaml_load`; non-mapping YAML roots fail fast.
2. CI now treats type checks as blocking via Pyright and runs the full pytest suite.
3. Self-upgrade regression/security runners fail closed when required tests are missing.
4. Admin, Browser, Plugin and SQLite boundaries are hardened:
   - Admin local no-token bypass defaults to disabled.
   - Browser DevTools calls are serialized with an async lock and use lazy `httpx` import.
   - Plugin names, entrypoints and in-process plugins are restricted; subprocess plugins run isolated.
   - SQLite stores use WAL, busy timeout and atomic approval decision transitions.

Note: pushing `.github/workflows/ci.yml` requires a GitHub token or App permission with workflow write scope.
