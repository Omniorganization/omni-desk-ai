# Omni Desktop App

Tauri shell for the local Agent runner.

Primary duties:

- Register the desktop device with `/app/devices/register`.
- Send runtime heartbeat to `/app/runtime/desktop/heartbeat`.
- Reflect pending approvals and task state from `/app/bootstrap` and `/app/sync`.
- Keep local execution on the desktop while Mobile App/Web Admin perform approval and governance.

This template intentionally does not embed unrestricted shell or file automation in the UI. Local execution must stay behind Omni Runtime permission gates.
