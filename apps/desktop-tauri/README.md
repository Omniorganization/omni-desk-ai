# Omni Desktop App

Tauri client that executes approved, scoped desktop tasks and chats with the gateway. It is not an agent runner: it plans nothing and calls no model locally.

Primary duties:

- Register the desktop device with `/app/devices/register`.
- Send runtime heartbeat to `/app/runtime/desktop/heartbeat`.
- Reflect pending approvals and task state from `/app/bootstrap` and `/app/sync`.
- Claim approved tasks it can execute, run their action, and report the outcome (`src/executor.ts`).

## What a desktop task can do

A gateway task is executable only if it carries one scoped action, validated by
the gateway, shown to the approver, and bound to the approval by its hash:

| Capability | Scope | Effect |
| --- | --- | --- |
| `workspace.read_file` | `workspace` (a directory under `~/OmniDesktopWorkspace`, `.` for the root), `relative_path` | reads one regular file of at most 1 MiB |
| `workspace.list_directory` | same | lists up to 1000 entry names |

Paths are resolved by the Rust commands, which refuse absolute paths, `..`,
symlinks and anything outside `~/OmniDesktopWorkspace`. Nothing writes files,
runs commands or uses the network. The desktop advertises these capabilities, so
the gateway hands it only tasks it can run; before running one it recomputes the
action hash and refuses a task whose action differs from the approved one.

The outcome is reported with `/app/tasks/{task_id}/status`: `completed` only
when the action ran, otherwise `failed` with the reason (no approval, no action
— free-text requests are not executable —, a hash mismatch, or a refused path).
The file contents or entry names go into the task's `result` (at most 64 KiB,
readable by the task's creator only); the organization-wide status summary
names neither the path nor the contents. There is no button that marks a task
done: "执行下一个任务" runs the same claim → execute → report cycle as the
automatic worker.

The operator token and device-signed requests go only to the gateway confirmed with "连接 Gateway" (or the one a previous confirmed connect saved to the keychain). While the connection form shows anything else, heartbeat, sync and task claims pause until the change is confirmed, which registers the device with that gateway again.

This template intentionally does not embed unrestricted shell or file automation in the UI. Local execution must stay behind Omni Runtime permission gates.
