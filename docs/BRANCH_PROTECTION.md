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
