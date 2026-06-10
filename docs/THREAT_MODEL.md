# Threat Model

Primary risks:

- Webhook spoofing and replay.
- Prompt-injected tool execution.
- Plugin supply-chain compromise.
- File path traversal.
- Self-upgrade privilege escalation.
- Long-term memory leakage of PII or secrets.

Primary controls:

- Signature, replay, rate-limit and idempotency checks.
- Per-action approval scopes.
- Out-of-process signed plugins.
- Workspace path confinement.
- PR-only upgrade workflow.
- Memory redaction before persistence.
