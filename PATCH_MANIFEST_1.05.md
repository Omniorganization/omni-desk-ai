# Patch Manifest 1.05

Version: `1.05+production-ga-closure`

## Closed blockers

- Unified project, Docker, workflow, Helm, Web Admin, Desktop, Mobile, and shared API contract versions to 1.05.
- Resolved Helm digest-gate conflict by keeping `values.yaml` as a safe source template and adding `scripts/render_locked_helm_values.py` plus release workflow validation for release-rendered locked values.
- Updated Kubernetes contract validation to check source templates separately from locked production values.
- Fixed Docker production AppSync configuration: HA storage now requires Postgres AppSync with production-safe idempotency and device enrollment settings.
- Reworked PostgreSQL AppSync startup/persist path to hydrate and persist normalized tables rather than relying on compact production state payloads.
- Removed JSON/in-memory fallback from Postgres task status updates when the task is absent from the DB source of truth.
- Normalized CI pytest entrypoints to `PYTHONPATH=. python -m pytest` to avoid runner-specific import behavior.
- Added native release lockfile contracts for Desktop Rust and Mobile Flutter source packages.
- Added `release/production-evidence.manifest.json` to make external production evidence explicit and auditable.

## Validation

- Version consistency gate: passed.
- Kubernetes source contract: passed.
- Locked Helm values renderer and locked-values contract with synthetic digests: passed.
- Enterprise readiness contract: passed.
- Tri-app source readiness contract: passed.
- GA release gate: passed.
- GA14-GA17 + 1.05 blocker tests: passed.
- Test collection: 404 tests collected.
- Chunked/file-level pytest validation completed successfully during local repair; single all-in-one pytest command exceeded this environment's wall-time limit.

## External evidence boundary

This source package does not fabricate external evidence. macOS notarization, Windows code signing, Android/iOS signing, APNS/FCM live push, registry attestation, multi-instance soak, rollback, and backup/restore drills must be produced by the corresponding signer, registry, cloud, or staging systems and attached to the release evidence manifest.
