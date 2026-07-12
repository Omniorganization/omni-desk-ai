# Industrial GA remediation (2026-07-12)

Source baseline: `main@bba64dcf8d4ddfa64e5bfd23291bc08020b161b0`.

## Changes

- Add audited, bounded SSE delivery with event IDs, disconnect handling, timeout,
  concurrency control, persisted complete responses, usage, trace and terminal events.
  This is not represented as provider-native token streaming.
- Make the desktop worker single-flight and derive advertised device capabilities
  from the registered executors.
- Mark unimplemented Web and Mobile controls unavailable instead of presenting
  them as working actions, and remove Web inline styles.
- Require the Web Node base image to be supplied by immutable digest and resolve
  that digest in the remote release job.
- Separate container liveness (`/health`) from readiness (`/ready`).

## Risk and rollback

- SSE holds one bounded request while the audited answer is generated. Roll back
  the route and API contract together if proxy or client compatibility regresses.
- Worker single-flight can reduce throughput to one task per desktop, by design.
  Roll back the App change only if lease-aware server scheduling proves sufficient.
- Web image builds now fail closed unless `NODE_BASE_IMAGE` is supplied. Roll back
  the Dockerfile and workflow together; never restore an unrecorded mutable base
  in a customer-distribution build.
- Liveness remains independent of database readiness. Roll back only if a runtime
  no longer exposes `/health`, and retain the semantic separation.

## Evidence policy

GitHub Actions is the validation environment for this change. Local builds and
tests are intentionally not used as GA evidence. Customer-distribution GA remains
blocked until repository- and commit-bound signed artifacts, push receipts,
provider smoke, PostgreSQL soak, rollback, restore and failure-injection evidence
are produced by the real remote systems and pass the non-audit evidence gate.
