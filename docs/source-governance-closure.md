# Source Governance Closure

This document defines the source-only closure boundary for the following dimensions:

- source engineering
- release governance
- security and supply chain
- tri-app engineering
- offline sync and reconnect

The source-only target is validated by `scripts/check_source_maturity_closure.py`. Customer-distribution GA is a stricter boundary and still requires real external evidence from signing systems, native stores, push providers, production-like soak tests, rollback drills, backup/restore drills, and failure-injection drills.

The repository must not convert source-only evidence into a customer-distribution GA claim unless `scripts/check_external_ga_evidence.py` passes against real, non-mock evidence.
