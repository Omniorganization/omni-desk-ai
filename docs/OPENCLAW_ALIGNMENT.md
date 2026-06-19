# OpenClaw-Aligned Interaction Layer

OmniDesk 0.7.36/0.7.39 keeps the OpenClaw-inspired product interaction layer and promotes the requested messaging surfaces to native, configurable adapters without weakening OmniDesk's security architecture or industrial deployment route.

## Adopted Product Ideas

- Local gateway remains the control plane; the user-facing product is the assistant across channels.
- A channel ecosystem catalog distinguishes native OmniDesk adapters from OpenClaw reference channels.
- The planner records and reuses per-channel, per-actor interaction preferences.
- UI Bridge can target reference-channel desktop apps while still requiring normal approval.

## Non-Adopted Security Model

- No OpenClaw runtime code is vendored.
- Reference channels are not treated as native support.
- Unknown or reference channels do not bypass webhook signatures, sender allowlists, OAuth scope limits, permission approvals, dual approval, sandboxing, or audit logs.
- Production readiness continues through OmniDesk's release hygiene, workflow pinning, deployment, observability, backup, and SLO checks.

## New Runtime Surfaces

- `omnidesk_agent.channels.ecosystem` exposes `channel_matrix()`, `resolve_channel()`, `recommend_surface()`, and `ecosystem_security_summary()`.
- `omnidesk_agent.learning.interaction_profile` infers interaction signals and confidence from task text plus learned profile.
- `ExperienceStore.record_interaction_profile()` persists successful and failed channel interaction outcomes by `channel:actor`.
- `/admin/channels/ecosystem` exposes a read-only catalog and security summary for operators.
