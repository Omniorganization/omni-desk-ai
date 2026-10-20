# Self-Learning Loop Upgrade

This upgrade turns Omni-deskAi from simple task logging into a safer learning loop.

## New Learning Flow

```text
task result
  -> ExperienceExtractor
  -> ExperienceMemory structured table
  -> FailureAnalyzer
  -> Metrics
  -> GrowthPlanner
  -> DailySelfLearningJob
  -> UpgradeProposal / SkillProposal
  -> ApprovalGate
  -> Sandbox Test
  -> Security Check
  -> Human Review
```

## Added Modules

```text
omnidesk_agent/learning/
  failure_analyzer.py
  experience_extractor.py
  growth_plan.py
  daily_job.py

omnidesk_agent/self_upgrade/
  approval_gate.py
  sandbox_runner.py
  security_checker.py
  rollback.py
  skill_sandbox.py
```

## Key Safety Rules

- Low-risk learning can create reports, skills, tests and proposals.
- Core runtime, permission, planner, shell and security changes require human approval.
- Auto-merge, force push, auto-restart and disabling approval are forbidden.
- Upgrade tests run through an argv-based sandbox runner.
- Skills are preferred over core code modifications.

## CLI

```bash
omnidesk learning-report --days 7
omnidesk metrics --days 7
omnidesk experience-search "xiaohongshu captcha" --limit 5
```
