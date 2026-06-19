# OmniDesk 0.7.39 Tri-App Foundation

OmniDesk 0.7.39 turns the 0.7.36 native-channel runtime into a productizable three-client platform:

1. **Desktop App** — local Agent runner and desktop runtime controller.
2. **Mobile App** — approval, chat, notification, and task-status client.
3. **Web Admin** — enterprise management console.

The three clients share a single `/app/*` Gateway API and a single sync timeline so business data does not fork between app surfaces.

## Product boundary

The apps are control surfaces. They do not bypass existing OmniDesk controls:

- Shell, browser, files, Gmail, UI Bridge, and channel send still remain behind permission gates.
- High-risk and desktop-runtime tasks can generate app approvals.
- Approval decisions are written back to the shared task timeline.
- Desktop runtime execution stays on the Desktop App / local daemon side, not inside the Mobile App.
- Web Admin is governance-oriented, not an unrestricted execution surface.

## Shared data business line

```text
Desktop App / Mobile App / Web Admin
              ↓
        Omni Gateway /app/*
              ↓
 AppSyncStore + existing Runtime stores
              ↓
 Conversation → Message → Task → Approval → Notification → Audit/Sync Event
              ↓
 Desktop runtime heartbeat + task status updates
```

The V1 shared objects are:

- Organization
- UserProfile
- DeviceRecord
- ConversationRecord
- MessageRecord
- TaskRecord
- ApprovalRecord
- NotificationRecord
- RuntimeStatusRecord
- SyncEvent

## API surface

| Method | Endpoint | Role | Used by |
| --- | --- | --- | --- |
| GET | `/app/bootstrap` | viewer | all clients |
| POST | `/app/devices/register` | operator | all clients |
| GET | `/app/conversations` | viewer | mobile, desktop, web |
| POST | `/app/conversations` | operator | mobile, desktop |
| POST | `/app/conversations/{id}/messages` | operator | mobile, desktop |
| GET | `/app/tasks/{id}` | viewer | all clients |
| POST | `/app/tasks/{id}/status` | operator | desktop |
| GET | `/app/approvals` | viewer | mobile, web |
| POST | `/app/approvals/{id}/decide` | owner | mobile, web |
| GET | `/app/notifications` | viewer | all clients |
| POST | `/app/runtime/desktop/heartbeat` | operator | desktop |
| GET | `/app/sync` | viewer | all clients |
| WS | `/app/ws` | gateway-protected | all clients |

## Client folders

```text
apps/
├── desktop-tauri/       # Desktop Agent runner shell
├── mobile-flutter/      # Mobile approval/chat/notification shell
├── web-admin-next/      # Enterprise web admin console
└── shared/              # Shared app API contract
```

## Recommended deployment

- Use Web Admin for enterprise setup and governance.
- Use Desktop App for local runtime heartbeat and local automation control.
- Use Mobile App for approvals and notifications.
- Keep production Gateway behind TLS/VPN or an authenticated ingress.
- Use separate operator/owner/viewer tokens for the three app roles.

## Next hardening steps

1. Replace the V1 JSON state file with a PostgreSQL-backed AppSyncRepository.
2. Add signed short-lived app session tokens instead of long-lived admin tokens in clients.
3. Add APNs/FCM push adapters for mobile push fanout.
4. Add desktop packaged installer signing and notarization.
5. Add Web Admin RBAC screens and immutable audit ledger views.
6. Add e2e tests covering desktop heartbeat → mobile approval → task resume.
