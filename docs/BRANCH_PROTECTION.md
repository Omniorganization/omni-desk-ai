# Branch Protection Baseline

Enable these repository rules before promoting a release artifact to production:

1. Require pull requests before merging to the default branch.
2. Require CODEOWNERS review for all protected paths.
3. Require the CI, Security, Release Policy, Supply Chain, Docker Scan and Self Upgrade Gate workflows to pass.
4. Require the `release-policy` and `external-ga-evidence-contract` jobs before merging release, workflow, deploy, script, or security-sensitive changes.
5. Require signed commits or GitHub verified signatures.
6. Disallow force pushes and branch deletion.
7. Require conversation resolution before merge.
8. Restrict production promotion to the `production` environment with manual approval.
9. Require deployment artifacts to be digest-pinned and attested before promotion.

The source-controlled baseline is recorded in `.github/branch-protection.required.json`; the repository control plane must be configured to match it before a release artifact is promoted.

## Release Commit Workflow Coverage

The final `main` merge commit must have first-party push workflow evidence, not
only pull-request head evidence. The CI, Security, Release Policy, Tri-App
Quality Gate, Docker Image Scan, Self Upgrade Gate, and Supply Chain Standard
Verification workflows are expected to run on `push` to `main`. Artifact-level
supply-chain verification still runs only on release workflow dispatch/call
inputs; the `main` push path runs the source-level lockfile, install-policy, and
supply-chain standard contracts.

## Live Verification

Source-controlled policy is not enough for production promotion. Verify the live
GitHub control plane before promoting a candidate:

```bash
TEAM_GOVERNANCE_TOKEN=<app-installation-or-fine-grained-token> \
  python scripts/check_live_branch_protection_contract.py . \
  --repository Omniorganization/omni-desk-ai \
  --token-env TEAM_GOVERNANCE_TOKEN \
  --write-report release/external-evidence/control-plane/github-branch-protection-live.json
```

The v3 command combines legacy branch protection, applied branch rules, and the
full active Ruleset definitions. It fails closed when any API surface needed for
effective-policy evaluation is unreadable, or when required contexts, strict
status policy, approvals, stale and last-push review policy, CODEOWNERS review,
signed commits, linear history, admin enforcement, force pushes, deletion,
direct pushes, deployments, lock state, or bypass actors do not match
`.github/branch-protection.required.json`. `scripts/check_github_branch_protection_live.py`
remains a compatibility entrypoint for the same canonical checker.
