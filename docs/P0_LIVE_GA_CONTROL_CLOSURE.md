# P0 Live GA Control Closure

## Scope

This change closes source-side false-positive paths for three live P0 controls:

1. effective GitHub branch protection and Rulesets;
2. organization/team CODEOWNERS governance;
3. per-file binding of the current Release native payload to external signing evidence and Main Verification.

It does not claim that external Real GA evidence currently exists. Candidate workflows remain audit-only and must preserve blocked evidence in their artifacts.

## Enforcement boundary

- `scripts/check_live_branch_protection_contract.py` combines legacy protection, applied branch rules, and active Ruleset details. It checks exact required contexts, strict status policy, approvals, stale and last-push review requirements, Code Owner review, signed commits, linear history, deletion/force-push/direct-push controls, deployments, branch lock, admin enforcement, and bypass actors.
- `scripts/check_github_team_governance_live.py` verifies organization ownership, closed/visible teams, explicit repository permissions, exact team-only CODEOWNERS mappings, live team membership, reviewer independence from the checked commit author, and the effective branch report.
- `scripts/check_current_release_artifact_binding.py` rehashes the actual Android, iOS, macOS, and Windows distributable files in the current Release run. Every digest occurrence must match the current native manifest, external signer evidence, per-artifact attestation subject, and the selected Main Verification binding.
- `scripts/check_customer_distribution_ga.py` requires the current Release binding report as a final category. The Release workflow runs that checker before aggregate release-payload signing. Pre-release readiness only audits this Release-only category and cannot claim Customer GA.

## Risk

- Real GA will remain blocked until every required team contains at least two members with an independent reviewer and Actions has `OMNIDESK_GITHUB_GOVERNANCE_TOKEN` with repository Administration read plus organization Members read.
- Existing single-digest Main Verification artifacts are intentionally incompatible with the new per-artifact requirement. External signer evidence and Main Verification must be regenerated.
- Non-deterministic or unsigned rebuilds will not match external signed artifacts. The Release payload must use the exact platform-signed bytes represented by the evidence.
- Candidate checks return success only to permit source review; their JSON reports retain `status: blocked` and enumerate all missing live evidence.

## Rollback

Revert the source commit to restore the previous checkers and workflows. If the two newly required check contexts cannot run, update the repository Ruleset through the audited control-plane process to remove only `main-verification` and `team-governance`, then read back the effective rules. Do not disable signed commits, Code Owner review, strict checks, or the no-bypass policy.

## Verification evidence

- Python suite: `710 passed, 1 skipped`.
- Modified GitHub Actions workflows pass `actionlint`.
- Source contracts for branch protection, team governance, Main Verification, and Real GA readiness pass.
- Live effective `main` protection passes all 17 v3 assertions with 16 required contexts and no Ruleset bypass actors.
- Live team governance correctly remains blocked: all four required teams have explicit repository permissions but only one member and zero reviewers independent of the checked commit author.

Live reports are deliberately kept outside `release/external-evidence/` until produced by the approved workflows; local diagnostics are not committed as Real GA evidence.
