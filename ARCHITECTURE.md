# OmniDesk Architecture

OmniDesk is organized as a root-level monorepo so source, applications, infrastructure, tests, and release evidence are visible from the repository entrypoint.

## Repository Boundaries

- `omnidesk_agent/` contains the Python API gateway, agent runtime, channel adapters, model router, approval, audit, memory, self-healing, and self-upgrade enforcement code.
- `apps/web-admin-next/` contains the Web Admin console for chat, approvals, channels, devices, runtime state, and audit-facing operations.
- `apps/desktop-tauri/` contains the desktop control hub and local runtime shell for signed task execution.
- `apps/mobile-flutter/` contains the mobile chat, approval, notification, and task status client.
- `apps/shared/` contains the tri-app API contract used by Web, Desktop, Mobile, and backend tests.
- `packages/` documents the stable package ownership map over the current runtime modules.
- `deploy/` contains Docker, Kubernetes, systemd, launchd, sandbox-runner, and observability deployment assets.
- `infra/` exposes industrial infrastructure entrypoints that map to the deploy assets.
- `tests/` contains contract, security, release-governance, runtime, and tri-app tests.
- `release/` contains external GA evidence contracts, templates, audits, and production evidence manifests.

## Runtime Flow

1. Channels, Web, Desktop, or Mobile send requests into the FastAPI gateway.
2. The AppSync API records conversations, messages, tasks, approvals, device state, and notifications.
3. Chat requests route through the governed model router and persist user/assistant turns with provider metadata and audit trace IDs.
4. Tool and runtime actions are classified by risk and enforced through approval, signed device requests, and audit checkpoints.
5. Desktop and Mobile clients synchronize task, approval, and notification state through the shared `/app` contract.
6. Release, evidence, supply-chain, and deployment checks fail closed until real external evidence is present.

## Production Boundary

This repository can prove source-level gates, contracts, and package integrity. It is not a customer-distribution Production GA release until `scripts/check_external_ga_evidence.py .` passes without `--audit-only` against real signing, device, push, staging, rollback, backup/restore, and self-healing evidence.
