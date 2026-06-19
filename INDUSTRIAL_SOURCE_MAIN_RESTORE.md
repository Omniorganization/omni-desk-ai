# Industrial Source Main Restore Contract

This repository treats `main` as the source trunk. Release packages are outputs
of the trunk, not replacements for it.

## Main Branch Contract

- `main` must contain the buildable source tree, including `README.md`,
  `.github/workflows/`, `apps/`, `omnidesk_agent/`, `scripts/`, `tests/`,
  `deploy/`, and `release/`.
- Source-gated zip directories and package-only snapshots must be published as
  GitHub Releases, Actions artifacts, or backup branches. They must not be the
  only content on `main`.
- The package-only 1.11.8 state is retained outside the source trunk as
  `backup/package-only-1.11.8` when a remote backup branch is available.
- Any future package replacement must first prove that the source trunk remains
  recoverable from `main` or from an explicit source branch.

## Release Channel Boundary

- Candidate/source-gated releases may run
  `scripts/check_external_ga_evidence.py . --audit-only` and must keep the
  resulting report labeled as an evidence audit, not a GA pass.
- Real GA releases must run `scripts/check_external_ga_evidence.py .` without
  `--audit-only`. Missing evidence must fail the release.
- `distribution-ga-preflight` remains fail-closed through
  `external-ga-evidence-gate`.

## Real GA Evidence Required

Real GA cannot be claimed from repository structure or passing source-package
tests alone. The external evidence set must close signed native builds,
APNS/FCM delivery, Postgres-backed soak behavior, rollback and backup/restore
drills, fixed staging endpoints, and enterprise identity evidence such as
SSO/OIDC where in scope.

Until those external artifacts exist and the fail-closed gate passes, the
highest honest rating is a strong source-gated candidate, not customer
distribution Production GA.
