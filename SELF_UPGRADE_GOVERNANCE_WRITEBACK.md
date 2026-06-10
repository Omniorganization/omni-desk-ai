# Self-Upgrade Governance Writeback

This patch closes the evidence loop for self-upgrade proposals.

`GovernedSelfImprovement.evaluate_proposal()` now writes these results back into each proposal's metadata:

```text
permission_diff
risk_classification
regression_result
security_result
shadow_result
canary_result
```

This makes every upgrade auditable from proposal to evaluation.

## CLI

```bash
omnidesk upgrade-evaluate <proposal_id>
```

## API

```text
POST /self-upgrade/proposals/{proposal_id}/evaluate
```

The route is protected by AdminAuth.

## Policy

- Regression/security checks must pass before canary.
- Permission expansion blocks automatic canary and requires human approval.
- Low-risk prompt/skill proposals may enter canary.
- Code/permission/deployment proposals never auto-merge.
