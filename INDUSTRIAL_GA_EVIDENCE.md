# OmniDesk Industrial GA Evidence

Version: `1.12.5+root-monorepo-production-ga-candidate`

Status: Production GA candidate, not a final Real Production GA attestation.

## Code-Level Controls

- API resource guard is in the HTTP request path and supports a Postgres backend for shared rate state.
- Trusted proxy configuration is explicit; empty `trusted_proxy_ips` means forwarded client IP headers are ignored.
- High-risk execution, approval, break-glass, and self-upgrade POST routes use Pydantic request schemas.
- High-risk state-changing POST routes require `idempotency-key` before execution.
- `/agent/run` uses a persistent idempotency store that binds key scope to actor, source device, route, and message payload hash; exact replays return the first response and payload mismatches fail with conflict.
- Model budget preflight uses a server-side pricing table, not caller-provided `projected_cost_usd` metadata.
- Model cost ledger records a server-side cost estimate when a provider omits cost metadata.
- Production validation requires persistent model budget ledger and Postgres storage for durable shared cost state.
- Production validation requires Postgres API resource guard backend.
- Gmail compose is opt-in, Gmail token encryption defaults closed for production validation, and OAuth state is server-generated.

## Generated Evidence Files

- `examples/config.production.yaml`: production preflight configuration with Postgres storage, Postgres resource guard, persistent budget ledger, remote sandbox, and explicit production secrets.
- `industrial_score.json`: machine-readable candidate score and remaining Real GA blockers.
- `omnidesk production-check --config examples/config.production.yaml`: one-command production configuration gate.

## Required External Evidence Before Real GA

- Latest GitHub Actions CI workflow is green on the final release commit.
- Latest GitHub Actions security workflow is green on the final release commit.
- Web Admin, Desktop, Mobile, Agent Gateway, and AppSync smoke tests pass against the same staged backend.
- Multi-instance Postgres resource guard and model budget abuse tests pass under load.
- Release artifact checksums match generated SBOM.
- Cosign signature verification passes for release artifacts and container images.
- SLSA provenance verification passes for release artifacts.
- Rollback, backup/restore, and failure-drill evidence is attached to the release.

## Boundary

This file records repository-level readiness controls. It does not prove that an external deployment, GitHub Actions run, signed release artifact, SBOM, SLSA provenance, or multi-instance load test has completed unless those external artifacts are attached to the release.
