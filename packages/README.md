# Packages

The current implementation keeps Python runtime code in `omnidesk_agent/` while exposing stable ownership boundaries through package directories. New shared libraries should graduate into these directories only when they need independent APIs, versioning, or ownership.

| Package Boundary | Current Source Owner |
| --- | --- |
| `agent-runtime` | `omnidesk_agent/core`, `omnidesk_agent/server.py`, `omnidesk_agent/appsync` |
| `connector-sdk` | `omnidesk_agent/channels`, `apps/shared/omni-app-api.contract.json` |
| `policy-engine` | `omnidesk_agent/security`, `omnidesk_agent/validation`, `omnidesk_agent/privacy` |
| `approval-core` | `omnidesk_agent/security/approval_*`, `omnidesk_agent/appsync/routes.py` |
| `audit-core` | `omnidesk_agent/security/audit_worm.py`, `release/production-evidence.manifest.json` |
| `memory-core` | `omnidesk_agent/memory`, `omnidesk_agent/self_learning` |
