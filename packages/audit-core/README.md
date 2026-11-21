# Audit Core

Current source: `omnidesk_agent/security/audit_chain.py` (keyed audit hash chain, verifier, rotation), `omnidesk_agent/security/audit_worm.py` (signed checkpoints and chain anchors), audit event writers in AppSync, and `release/production-evidence.manifest.json`.

This boundary owns immutable audit events, audit checkpoints, evidence manifests, and trace IDs used across chat, approval, tool, and runtime flows.
