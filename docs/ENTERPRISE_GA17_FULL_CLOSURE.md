# OmniDesk AI 0.7.33+industrial-ga17-full-closure Full Closure

GA17 closes the remaining HA and supply-chain gaps found in GA16:

- Enterprise PostgreSQL dependency is installed from a hash-locked lockfile.
- The runtime no longer directly constructs SQLite-backed memory, token-budget, model-cost, or learning-experiment state in `daemon.py`.
- RepositoryFactory now owns all core runtime state stores.
- PostgreSQL runtime stores cover approvals, dual approvals, break-glass, webhook replay, jobs, outbound messages, runs, memory, token budget, model cost, and learning experiments.
- Kubernetes HA defaults are stateless application pods with external PostgreSQL as durable state.
- `/ready` and `/admin/ready` check database/runtime-store health, secrets, sandbox runner configuration, plugins, and schema readiness.
- Release governance tests read the canonical package version instead of hard-coding stale release strings.
