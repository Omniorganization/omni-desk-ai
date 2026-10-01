# Industrial readiness status — 2026-10-01

Baseline: `c4473962690b61332530ceb6607bc1dd98dbfc19`.

Current classification: **engineering GA candidate, not verified customer GA**.
The historical 94–96 score is a manual self-assessment, not a current audited
rating. `industrial_score.json` now labels that limitation explicitly.

This change enforces admission-before-body-read, bounded ingestion/telemetry,
disabled-channel rejection, non-executing permission decisions, and configured
read-only file capabilities. Helm startup and writable state paths are explicit.
The test tool also preserves configured sandbox isolation, and shared upgrade
test runners reject production argv execution instead of falling back to host.
OAuth approval/audit proposals retain code/state digests rather than raw
one-time credentials. Next.js is locked above the newly published advisory's
patched 16.3.6 floor; application exploit reachability was not established.
Security findings and source evidence are retained privately in Codex Security.

Compatibility changes: oversized bodies are rejected on all HTTP methods;
file writes require `capabilities.files.allow_write: true` plus ordinary approval;
dry runs return a non-executing result; snapshots retain bounded diagnostic
histogram samples while Prometheus bucket/count/sum values remain cumulative.

Verification is deliberately performed in GitHub Actions, not by running the
project locally. CI full coverage/atomicity, focused boundary tests, security
gates and iOS Simulator/Windows/macOS platform gates must all pass for the exact
PR commit. No check, coverage threshold, approval or release policy is lowered.

The standard audit reviewed 31 source files from the initial 1158-file inventory;
architecture mapping and search hits are not counted. Remaining source surfaces
need further review. Passing these checks is not a claim of zero vulnerabilities.

Customer GA still requires real release signing, commit-bound artifacts,
SBOM/provenance, external environment smoke/load/recovery evidence and device
distribution verification. Simulator and test-signed builds do not establish it.

Rollback: revert this PR through normal review and rerun required checks. Do not
disable authentication, approvals, capability policy or production evidence gates.
