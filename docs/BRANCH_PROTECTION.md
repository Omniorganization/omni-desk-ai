# Branch Protection Baseline

Enable these repository rules before promoting a release artifact to production:

1. Require pull requests before merging to the default branch.
2. Require CODEOWNERS review for all protected paths.
3. Require the CI, Security, Supply Chain, Docker Scan and Self Upgrade Gate workflows to pass.
4. Require signed commits or GitHub verified signatures.
5. Disallow force pushes and branch deletion.
6. Require conversation resolution before merge.
7. Restrict production promotion to the `production` environment with manual approval.
8. Require deployment artifacts to be digest-pinned and attested before promotion.

This document is intentionally operational rather than automated because branch protection is enforced by the GitHub repository control plane, not by the source tree itself.
