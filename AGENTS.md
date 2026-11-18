# OmniDesk Agent Rules

These rules apply to every AI or automated repair workflow in this repository.

- Do not lower security policy, sandbox policy, CSP, RBAC, or approval thresholds.
- The only permitted way to accept a known advisory in a dependency audit is a governed exception: the advisory has no usable fixed release, the npm project's audit runs through `scripts/check_npm_audit.py`, the exception is recorded in `release/security-exceptions/<GHSA>.md` with an owner, the audited project in its scope, an expiry at most 90 days ahead, upstream tracking, impact, runtime reachability, compensating controls and removal criteria, and the PR adding or renewing it is approved by a code owner. `allow-ghsas` and `--ignore` stay forbidden.
- Do not bypass approval, dual approval, device signatures, webhook signatures, audit logs, or evidence gates.
- Do not expand token scope, secret access, browser profile access, filesystem access, or network access without owner approval and tests.
- Do not delete or weaken the external GA evidence gate.
- Do not convert audit-only external evidence checks into passed customer-distribution GA.
- Do not fabricate release evidence, signed artifacts, push receipts, soak reports, rollback reports, backup/restore reports, or self-healing drill reports.
- All production changes must include tests, risk notes, rollback steps, and an evidence bundle.
- Self-healing and self-upgrade changes must run through an `ai/*` branch and PR review; never patch or merge directly to `main`.
