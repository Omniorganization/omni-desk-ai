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
GITHUB_TOKEN=<admin-token> python scripts/check_github_branch_protection_live.py . \
  --repo yinyufan0813-cmyk/omni-desk-ai \
  --write-report release/github-branch-protection-live.json
```

The command fails closed when the default branch is not protected, when required
status checks are missing, when CODEOWNERS review is not required, or when force
pushes/deletions/conversation-resolution controls do not match
`.github/branch-protection.required.json`.
