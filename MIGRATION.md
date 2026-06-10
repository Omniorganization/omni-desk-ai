# Migration Notes

## 0.5.0-beta.1

- Public `/health` no longer exposes runtime internals. Use `/admin/status` with AdminAuth.
- Webhook signatures are required for enabled channels.
- In-process plugins are rejected. Add `sha256` and `signature` to plugin manifests.
