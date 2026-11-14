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

## Plugin signatures

Every environment requires a v2 manifest signature. v2 is an HMAC (key: `OMNIDESK_PLUGIN_SIGNING_SECRET`) over the name, version, entrypoint, `sandbox`, `permissions`, `trusted` and the SHA-256 of every file in the plugin directory, so editing the manifest or any sibling module invalidates it. A plugin directory may contain only one of `plugin.yaml`, `plugin.yml` or `plugin.json`, and a plugin may not use the name of a built-in tool or of another plugin.

Sign or re-sign a plugin after reviewing its manifest fields and files (the signature vouches for whatever is there):

```bash
OMNIDESK_PLUGIN_SIGNING_SECRET=... python scripts/sign_plugin.py PLUGIN_DIR
```

If the manifest already carries a signature, the script re-signs only when that signature still verifies (for v1: the entrypoint digest), so migrating a plugin that was modified after signing fails with exit 1. Review the change, then re-sign with `--force`. The same applies after you edit a v2 plugin yourself.

`python scripts/sign_plugin.py --check PLUGIN_DIR` verifies without writing and exits 1 while the manifest is still v1 or does not verify, so it can be run over existing plugin directories before upgrading.

Legacy v1 signatures cover only the entrypoint digest. They are rejected unless `plugins.allow_legacy_v1_signatures: true` is set, which is a migration aid only:

- it is ignored in production (detected from the environment as well as from the runtime config);
- a v1 plugin cannot satisfy `plugins.trusted_only` (its `trusted` flag is unsigned), so it also needs `trusted_only: false`;
- the unsigned manifest fields are discarded: the plugin runs untrusted, in the docker sandbox, with no permissions, and a warning is logged on every load. Its name, which `plugins.allowlist` matches, is unsigned as well.

Remove the key from the config before rolling back to a release that does not know it (the config schema rejects unknown keys).

## Shell command policy

`security/command_policy.py` is shared by the shell tool, the self-upgrade runner and the remote sandbox runner:

- `git branch` is allowed for listing only (`-a`, `-r`, `-v`, `--list <pattern>`, `--show-current`, `--merged`, `--contains=...`, ...). Creating, deleting, renaming, copying, force-moving or re-tracking a branch is refused and is never classified as read-only.
- `git pull` (upgrade profile) takes no repository, or `origin` with at most one plain branch name and a few merge-mode flags; URLs, other remotes, refspecs with `:` or `+` and strategy/upload options are refused, matching the `git push origin HEAD:<prefix>/...` restriction.
- `git pull origin` / `git push origin` are refused while a file or directory named `origin` or `origin.git` exists in the workspace root, because git would use that path if no `origin` remote is configured.
- No argument or option value may be absolute, contain a `..` segment, or contain a `.git` segment (case-insensitive, also `.git.`, `.git ` and `GIT~1`). When the workspace root is known, symlinks are resolved and a value that resolves outside the workspace or into `.git` is refused, the same rule the files tool applies to writes.
- For tools other than git, values are also refused if they start with `~` or contain `$VAR`/`${VAR}` (pytest expands both in path options), or if a path after `:`, `=` or `,` breaks the rule above (for example `--cov-report=xml:<path>`). Git arguments such as commit messages and `<rev>:<path>` are checked as a whole.
- pytest `@file` argument files and `compileall -i <list>` are refused: they read more arguments from a file that the policy never sees.
- At execution time git also gets `-c sequence.editor=:`, `--upload-pack=git-upload-pack` (pull), `--receive-pack=git-receive-pack` (push) and `--no-ext-diff` (diff), and `--ext-diff` is refused, in addition to the existing fsmonitor/hooks/ext-transport/editor overrides. Repository config remains trusted input for anything else (credential helpers, `core.sshCommand`, signing programs): the agent cannot write `.git`.

## Vision tool

`vision.ground` only reads `image_path` inside the workspace root. Relative paths are resolved against the process directory as before (this is how the computer tool reports screenshots when `workspace.root` is relative); the resolved path must stay inside the workspace, so symlinks leaving it are refused. The file must be a regular `.png`, `.jpg`/`.jpeg`, `.gif` or `.webp` file of at most 10 MiB whose content matches its type. The checks run before the approval request and again after it, and the approved path must not have changed. Images from other locations (for example a UI bridge that saves screenshots elsewhere) must be copied into the workspace first.

## Browser control policy

Chrome DevTools control is a high-risk capability because it can interact with already-authenticated browser sessions. Browser actions are fail-closed when `channels.chrome.allowed_origins` is empty. Each controlled tab is bound to the first actor that uses it, and high-risk URLs such as banking, payment, ads manager, admin, and console pages are escalated to `critical` approval risk. Approval payloads include the current URL, origin, title, selector, actor, and expected result where available.

## RC7 remote sandbox requirement

For production Docker Compose/Kubernetes deployments, use `sandbox.backend=remote_docker` instead of local Docker execution inside the application container. The app container must never mount `/var/run/docker.sock`. Local `sandbox.backend=docker` is only acceptable on a dedicated runner host where the OmniDesk app process itself is the sandbox runner and no application secrets or user data are colocated.
