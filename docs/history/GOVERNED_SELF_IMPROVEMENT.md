# Governed Self-Improving Agent

This upgrade moves Omni-deskAi from `Self-Learning Agent` toward a `Governed Self-Improving Agent`.

## Pipeline

```text
Daily Review
  -> Failure & Opportunity Detection
  -> Upgrade Proposal Generation
  -> Upgrade Scoring
  -> Risk Classification
  -> Permission Diff Check
  -> Prompt / Workflow / Code Review Artifact Generation
  -> Sandbox Test
  -> Regression Test
  -> Shadow Mode
  -> Canary Release
  -> Human Approval
  -> Stable Release
  -> Post-Upgrade Evaluation
  -> Upgrade Memory
```

## CLI

```bash
omnidesk upgrade-proposals
omnidesk upgrade-proposals --status pending
omnidesk upgrade-artifact <proposal_id>
omnidesk upgrade-feedback <proposal_id> approved --reason "Looks safe"
omnidesk upgrade-feedback <proposal_id> rejected --reason "Email sending must always require approval"
```

## Dashboard

```text
/self-upgrade/dashboard
/self-upgrade/proposals
```

## Hard Rules

- No proposal, no upgrade.
- No approval, no core merge.
- Permission expansion is always flagged.
- Shadow/canary are preferred before stable release.
- Upgrade effectiveness and human feedback are remembered.
