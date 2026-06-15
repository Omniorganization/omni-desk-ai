# Running GA Verification Checklist

This checklist moves OmniDesk from code-level GA to runtime-level GA. Every item should be executed against the real GitHub `staging` and `production` environments before claiming full unattended production readiness.

## Release and supply chain

- Run `release.yml` with a digest-pinned OCI image.
- Verify `dist/release_metadata.json` contains the expected package version, artifact SHA-256, build SHA, and image digest.
- Verify GitHub artifact attestations for wheel, checksums, SBOM, release metadata, and SLSA provenance.
- Verify Cosign keyless signatures for the wheel, checksums, SBOM, and release metadata.
- Verify Cosign SLSA / in-toto attestation for the wheel.

## Staging

- Run `deploy-staging.yml` using the signed release artifact.
- Confirm smoke checks validate `version`, `artifact_sha256`, `build_sha`, and `image_digest`.
- Confirm `/admin/metrics` and `/admin/slo` are available and healthy.
- Confirm the sandbox runner health, ready, and strict run checks pass.

## Production

- Run `promote-production.yml` from the immutable release artifact run id.
- Confirm production deploy mode is not `noop`.
- Confirm production smoke validates runtime identity.
- Confirm production SLO gate blocks an intentionally unhealthy metrics fixture.

## Rollback

- Run `rollback-drill.yml` with current and previous artifacts.
- Confirm current deployment smoke succeeds.
- Confirm rollback to previous artifact succeeds.
- Confirm previous runtime identity matches previous artifact metadata.

## Backup and recovery

- Run `backup-restore-drill.yml` with encrypted backup enabled.
- Confirm backup verification checks encryption, checksums, and SQLite `PRAGMA quick_check`.
- Confirm restore RPO/RTO measurements are recorded.
- Confirm backup age / freshness alerts trigger on stale backup fixtures.

## Soak / chaos

- Run `soak-test.yml` manually with a long duration before unattended production.
- Confirm no SQLite ResourceWarning, connection leak, queue backlog growth, or sandbox timeout spike.
- Confirm webhook replay and approval race rejections are counted and bounded.

## Observability

- Import `deploy/observability/grafana-dashboard.json`.
- Load `deploy/observability/prometheus-rules.yml`.
- Confirm alert routing and runbook links are available for the on-call operator.
