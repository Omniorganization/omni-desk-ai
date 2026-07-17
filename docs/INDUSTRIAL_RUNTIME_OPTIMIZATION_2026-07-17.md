# Industrial Runtime Optimization — 2026-07-17

This change closes the highest-value source-level optimization gaps identified after the
dependency-upgrade merge train.

## Implemented

- Bounded shared PostgreSQL connection pool for core state and transactional outbox.
- Cached `PostgresRuntimeStateStores` and outbox repositories per runtime factory.
- SQL-filtered, `FOR UPDATE SKIP LOCKED LIMIT 1` queue/outbound claims.
- Indexed approval lookup and ready-work selection.
- Five-second shallow readiness cache executed outside the ASGI event loop.
- Separate deep authenticated readiness path.
- PostgreSQL pool pressure metrics exposed through runtime/readiness status.
- Canonical channel adapter instances reused by user-facing aliases.
- Repository factory added to deterministic runtime resource cleanup.

## Fail-closed boundary

No external evidence is fabricated. Customer-distribution Real GA still requires the
existing signed artifacts, real-device validation, live push delivery, PostgreSQL soak,
rollback, backup/restore, and disaster-recovery evidence.
