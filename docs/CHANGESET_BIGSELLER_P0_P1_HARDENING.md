# BigSeller P0/P1 Hardening Changeset

This changeset closes the source-level P0/P1 gaps identified after PR #26 merged.

## Completed

- Enforced `BIGSELLER_WEBHOOK_MAX_BODY_BYTES` with HTTP 413 rejection.
- Added TTL and purge support for BigSeller idempotency records.
- Added SQLite migration compatibility for `expires_at`.
- Added PostgreSQL `expires_at` column and purge query.
- Converted dead-letter reporting to a current gauge: `bigseller_dead_letter_current`.
- Added BigSeller live smoke as a required external GA evidence category.
- Added `release/external-evidence/templates/integrations/bigseller-live-smoke.template.json`.
- Added enterprise dependency contract verification for `requirements.enterprise.lock`, `psycopg`, `psycopg-binary`, and Dockerfile production install wiring.
- Wired enterprise dependency verification into Release Policy and Main Verification.
- Expanded BigSeller production hardening tests.

## Boundary

This changeset still does not create real BigSeller live evidence. The customer-distribution GA gate now explicitly requires `release/external-evidence/integrations/bigseller-live-smoke.json` generated from an approved BigSeller staging or production environment. Template files are not accepted as evidence.

## Version policy

The version remains `1.12.7+root-monorepo-production-ga-candidate` in this changeset to avoid a partial version bump that would fail the repository-wide `check_version_consistency.py` gate. A clean version bump should be done as a separate release-management PR that updates all Python, Docker, workflow, Web, Desktop, Tauri, Flutter, Helm, and manifest version surfaces together.
