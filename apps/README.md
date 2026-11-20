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
| Web Admin | governance endpoint | approve or reject, device list and revoke, starting a desktop enrollment, the AppSync activity feed, channel status, chat; no user management or security-audit-log view yet |

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

## API contract

`apps/shared/omni-app-api.contract.json` is the contract between the gateway
and the three clients:

- **Versioned.** `contract_version` is `MAJOR.MINOR`. Every client sends
  `X-OmniDesk-Contract`, and the gateway answers every contract route with its
  own version. A client of another major is refused with `426
  contract_version_unsupported`. A client refuses a gateway of another major,
  and says to update. Minor versions only add routes or optional fields.
- **Payload schemas.** Each route a client calls has a request and a response
  schema, listing what the clients send and read; shared records are in
  `schemas`. `tests/test_contract_payload_schemas.py` drives every client route
  on a real gateway and validates both bodies.
- **Checked by the clients.** `scripts/generate_contract_clients.py` writes
  `desktop-tauri/src/generated/contract.ts`, `web-admin-next/lib/generated/contract.ts`
  and `mobile-flutter/lib/generated/contract.dart`. These contain the version and,
  per route, the field paths and types the client relies on. Each client checks
  every gateway answer against them (Web Admin in its server-side proxy), and
  each client's tests check the request bodies it sends. Regenerate after editing
  the contract.
- **Gates** (CI job `backend-and-contract`):
  - `check_contract_bidirectional_diff.py` fails on any drift between the
    contract, the gateway's routing table and the paths the clients call.
  - `generate_contract_clients.py --check` fails on a stale generated module.
  - The schema and negotiation tests.
- **Not done:** response types are checked at run time, not compile time. The
  clients' API methods still return `any` / `Map`.
