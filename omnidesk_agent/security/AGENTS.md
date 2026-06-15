# Security Package Rules

- Fail closed when identity, signature, approval, policy, or evidence state is missing.
- Do not add permissive defaults for production mode.
- Any policy expansion must include tests for denial, approval-required, and allowed paths.
- Break-glass behavior requires owner approval, dual approval where configured, full audit, and rollback notes.
