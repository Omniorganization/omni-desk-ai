# Account-independent cloud acceptance

`Isolated Cloud Acceptance` runs on an ephemeral GitHub-hosted Ubuntu VM. It uses
the existing production configuration template, production validation, real
Uvicorn HTTPS sockets, PostgreSQL 16 and an unprivileged rootless Podman runner.
Only loopback listeners are created. No Oracle tenancy or new provider account is
required. Public-repository standard Actions runners have no runner-minute charge.

The script requires `GITHUB_ACTIONS=true` and generates short-lived, synthetic role
credentials and a private TLS certificate. The client verifies the exact trusted
certificate and hostname. Credentials, TLS keys, config and database backup bytes
are not uploaded. Artifacts contain redacted process logs, checks, checkout SHA,
run ID, backup hash and artifact hashes. PostgreSQL's Docker service is a database
fixture; sandbox workloads must use rootless Podman and the digest-pinned image.

Checks exercise missing/invalid authentication, viewer/operator permissions,
message persistence across a real server restart and recovery into a separate
database. A 30-request probe is explicitly short; it is not a long soak. Existing
strict sandbox probes exercise real container execution and negative controls.
The script fails on unavailable runtime support rather than weakening isolation.
Synthetic Ed25519 keys exercise actual enrollment challenges, signed device token
rotation and rejection of unsigned/replayed requests. These are protocol tests
over HTTPS; they are not a native publisher certificate or a physical device
connected to this private loopback service.

## Risk and rollback

This adds an opt-in/path-triggered CI acceptance workflow and synthetic verification
script. It does not change deployment credentials or production configuration.
Pulling the pinned sandbox image needs public registry availability. Ubuntu's
rootless cgroup/user namespace support and PostgreSQL startup are real dependencies;
their failures must remain visible. Stop or revert this workflow and script to
roll back. Each job VM, subprocess and private temporary fixture is disposable.

## Evidence boundary

Successful CI is private temporary runtime acceptance. It proves neither a durable
public host, independent staging/production, HA, production publisher signatures,
real device enrollment/push, a paid model contract nor customer GA. BrowserStack
physical-device launch evidence is produced separately and bound to its own APK
and source SHA. The external GA evidence gate stays unchanged.
