# Sandbox Security Notes

## Docker sandbox hardening

Shell, plugin, and self-upgrade Docker execution use a restrictive profile:

```text
--network none
--read-only
--tmpfs /tmp:rw,noexec,nosuid
--user 65534:65534
--cap-drop ALL
--security-opt no-new-privileges
--pids-limit 128
--log-driver none
--pull never
```

Read-only shell commands mount the workspace read-only. Write-capable commands require the upgrade shell profile and remain subject to approval.

## Docker socket warning

Do not expose `/var/run/docker.sock` to the OmniDesk application container. A container with Docker socket access can usually escape to host-level control. Use a dedicated rootless sandbox runner when containerized OmniDesk must launch Docker sandboxes.

## Plugin sandbox policy

Production plugin execution must use `sandbox: docker`. `subprocess` is retained for development and test only, because it cannot provide strong namespace, network, memory, or syscall isolation.

## Browser control policy

Chrome DevTools control is a high-risk capability because it can interact with already-authenticated browser sessions. Browser actions are fail-closed when `channels.chrome.allowed_origins` is empty. Each controlled tab is bound to the first actor that uses it, and high-risk URLs such as banking, payment, ads manager, admin, and console pages are escalated to `critical` approval risk. Approval payloads include the current URL, origin, title, selector, actor, and expected result where available.

## RC3 remote sandbox requirement

For production Docker Compose/Kubernetes deployments, use `sandbox.backend=remote_docker` instead of local Docker execution inside the application container. The app container must never mount `/var/run/docker.sock`. Local `sandbox.backend=docker` is only acceptable on a dedicated runner host where the OmniDesk app process itself is the sandbox runner and no application secrets or user data are colocated.
