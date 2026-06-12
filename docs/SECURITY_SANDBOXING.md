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

## RC3 remote sandbox requirement

For production Docker Compose/Kubernetes deployments, use `sandbox.backend=remote_docker` instead of local Docker execution inside the application container. The app container must never mount `/var/run/docker.sock`. Local `sandbox.backend=docker` is only acceptable on a dedicated runner host where the OmniDesk app process itself is the sandbox runner and no application secrets or user data are colocated.
