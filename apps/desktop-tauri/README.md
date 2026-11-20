# Omni Desktop App

Tauri client that executes approved, scoped desktop tasks and chats with the gateway. It is not an agent runner: it plans nothing and calls no model locally.

Primary duties:

- Register the desktop device with `/app/devices/register`, and become challenge-verified through an owner's enrollment (below).
- Send runtime heartbeat to `/app/runtime/desktop/heartbeat`.
- Reflect pending approvals and task state from `/app/bootstrap` and `/app/sync`.
- Claim approved tasks it can execute, run their action, and report the outcome (`src/executor.ts`).

## Enrollment

A production gateway accepts a desktop's signed calls (heartbeat, claims,
status reports) only from a challenge-verified device, so a fresh install has
to be enrolled once:

1. An **owner** starts an enrollment for a desktop — `POST
   /app/devices/enrollment/start` with `device_type: "desktop"` and a pairing
   code of at least 8 characters — and hands the operator the enrollment id
   (`enroll_` + 16 hex digits) and the pairing code.
2. The desktop connects; when the gateway answers
   `device_not_challenge_verified`, it shows the "设备注册" panel and pauses
   its background heartbeat and claims.
3. The **operator** enters the id and code there. The desktop binds its device
   id and public key to the enrollment (`…/complete`), asks for a challenge
   (`…/challenge`), signs it with its device key and sends the signature
   (`…/verify`). It resumes once the gateway reports `challenge_verified`.

The challenge is signed by the native command `device_sign_challenge`: the
private key stays in the OS keychain and in Rust, and the command signs only
the gateway's challenge format
(`omnidesk-device-challenge:v2:chal_<16 hex>:hmac-sha256:device_challenge_nonce:<64 hex>`),
refusing any other input, so it cannot be used to sign requests. The gateway
records each step in its admin audit log and as `device.enrollment_*` /
`device.challenge_verified` events. Code: `src/enrollment.ts`,
`src-tauri/src/device_keys.rs` (`challenge_message`); tests:
`tests/enrollment.test.ts`, the Rust unit tests in `device_keys.rs`, and the
gateway protocol test `tests/test_desktop_gateway_e2e.py` at the repository
root.

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

The webview calls the gateway with `fetch`, so the gateway answers CORS for the
Tauri origins (`tauri://localhost`, `http://tauri.localhost`,
`https://tauri.localhost`) and for no other web page. It does not allow
credentials: the bearer token and the device signature remain what
authorizes a call. For `npm run dev`, start the gateway with
`OMNIDESK_DESKTOP_WEBVIEW_ORIGINS=http://127.0.0.1:5173`.

The operator token and device-signed requests go only to the gateway confirmed with "连接 Gateway" (or the one a previous confirmed connect saved to the keychain). While the connection form shows anything else, heartbeat, sync and task claims pause until the change is confirmed, which registers the device with that gateway again.

This template intentionally does not embed unrestricted shell or file automation in the UI. Local execution must stay behind Omni Runtime permission gates.
