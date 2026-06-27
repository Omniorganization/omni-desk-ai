# BigSeller Hardening Review Notes

This branch is intentionally scoped to source-level hardening after PR #26. It does not introduce real BigSeller endpoint paths or live evidence.

Review focus:

- webhook body size guard
- idempotency TTL purge
- dead-letter metric semantics
- BigSeller live evidence gate
- enterprise dependency gate
