# Omni Tri-App Foundation

This folder contains the productized clients for OmniDesk 1.11:

- `desktop-tauri/` — desktop client that executes approved, scoped workspace actions and reports their results.
- `mobile-flutter/` — mobile approval, chat, notification, and task-status shell.
- `web-admin-next/` — enterprise administration console for users, channels, approvals, audit, and runtime state.
- `shared/` — API contract shared by all client surfaces.

All clients use the same Gateway API namespace: `/app/*`.

## Runtime roles

| Client | Primary role | Capabilities |
| --- | --- | --- |
| Desktop App | execution endpoint for approved scoped actions | enrollment with an owner's pairing code, heartbeat, claiming tasks it can execute, reading files and listing directories in `~/OmniDesktopWorkspace`, reporting results |
| Mobile App | mobile control endpoint | chat, approval, notifications, task status |
| Web Admin | governance endpoint | user/device/channel/approval/audit/runtime management |

## Local development

Set a Gateway base URL and tokens in the client-specific environment/config files:

```bash
OMNI_GATEWAY_URL=http://127.0.0.1:18789
OMNI_OPERATOR_TOKEN=...
OMNI_OWNER_TOKEN=...
```

The back-end routes are implemented in `omnidesk_agent/appsync`.

## Quality and release gates

From the repository root:

```bash
make tri-app-quality PYTHON=.venv/bin/python
.venv/bin/python scripts/check_tri_app_release_readiness.py . --mode source
```

See `apps/RELEASE_CHECKLIST.md` for the signed build, push notification, device login, and installer checklist.
