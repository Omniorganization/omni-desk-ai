# Omni Desktop App

Tauri shell for the local Agent runner.

Primary duties:

- Register the desktop device with `/app/devices/register`.
- Send runtime heartbeat to `/app/runtime/desktop/heartbeat`.
- Reflect pending approvals and task state from `/app/bootstrap` and `/app/sync`.
- Keep local execution on the desktop while Mobile App/Web Admin perform approval and governance.

The operator token and device-signed requests go only to the gateway confirmed with "连接 Gateway" (or the one a previous confirmed connect saved to the keychain). While the connection form shows anything else, heartbeat, sync and task claims pause until the change is confirmed, which registers the device with that gateway again.

This template intentionally does not embed unrestricted shell or file automation in the UI. Local execution must stay behind Omni Runtime permission gates.
