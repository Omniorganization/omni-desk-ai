# Security Policy

## Supported Status

Current status: `1.12.7+root-monorepo-production-ga-candidate`.

This is a source-gated production GA candidate. It must not be represented as customer-distribution Production GA until external evidence gates pass against real systems.

## Required Controls

- Do not weaken approval, dual approval, sandbox, RBAC, CSP, webhook signatures, device signatures, audit logs, or external GA evidence gates.
- L2 and higher write/action capabilities require approval gating.
- L3 and L4 actions require human approval, auditability, timeout/revocation behavior, and rollback or containment evidence.
- Signed device requests are required for sensitive Desktop and Mobile runtime actions.
- Release artifacts must include portable checksums and release manifests.
- Customer-distribution releases require signed artifacts, SBOM/provenance, image digests, and real external evidence.

## Sandbox Isolation

What isolates code the agent runs (`omnidesk_agent/sandbox`, `plugins`, `tools/shell.py`):

| Backend | Isolation | Allowed in production |
| --- | --- | --- |
| `docker` (shell, tests, plugins) | Container. Flags: `--network none`, `--cap-drop ALL`, `no-new-privileges`, the runtime's default seccomp filter, read-only image filesystem, `noexec` `/tmp`, uid/gid 65534, pids 128, memory and CPU limits, no gateway environment. The workspace is mounted read-only for the default command allowlist; upgrade commands, when enabled, get a writable mount. | Yes, except inside the app container |
| `remote_docker` | The same container profile on a separate runner host. The runner requires a bearer token and an HMAC-signed, nonce-bound request, and refuses a rootful container runtime. | Yes (recommended) |
| `argv` (shell, tests) | None. A host process with a command allowlist, a minimal environment and git hardening. | No |
| `subprocess` (plugins) | None. A host process running as the gateway user, with isolated Python and a scrubbed environment. | No |

- Plugin and remote-runner work cannot outlive its time budget. Each such container is named, and a timeout or cancellation kills and removes it by name before its temporary workspace is deleted (`sandbox/container.py`). A subprocess plugin runs in its own process group, and the whole group is killed.
- OmniDesk's own state in `workspace.root` (approval, dual-approval, break-glass, run and memory databases, audit files, AppSync state, screenshots; `sandbox.isolation.RUNTIME_STATE_NAMES`) is never part of the archive sent to the remote runner. `runtime_state_masks()` hides those entries from a container that mounts the workspace.
- The remote runner requires request signing by default. Only a non-production runner may turn it off (`OMNIDESK_SANDBOX_REQUIRE_HMAC=0`), and it refuses to start without the HMAC secret. With `OMNIDESK_ENV=production` it also refuses the rootful-runtime override.
- CI (`security-attack-surface`) runs real containers with the argv the product builds and attempts each of the following; every attempt must fail:
  - reach the network or DNS;
  - write the image filesystem or a read-only workspace;
  - execute from `/tmp`;
  - setuid, chroot, mount or unshare;
  - exceed the pids limit;
  - read gateway secrets, or read or change masked state (writes to a mask go to `/dev/null`);
  - outlive a timeout.
  `OMNIDESK_REQUIRE_CONTAINER_TESTS=1` turns a missing runtime into a failure. The rootless runner itself is exercised in `isolated-cloud-acceptance`.
- Known limits:
  - Isolation rests on the container runtime and the host kernel; a kernel exploit breaks it.
  - The shell tool and the self-upgrade test runner are not yet switched to the named-container launcher or to the state masks. They live in other owners' paths, and the change has been requested.

## Reporting

Do not include secrets, credentials, private keys, tokens, customer data, or production evidence payloads in public issues. Use a private security channel controlled by the repository owner.
