# GA13 Runtime Enterprise Closure

This release closes the remaining enterprise-runtime gaps with explicit storage backend governance, OTLP runtime wiring, rootless sandbox-runner deployment contracts, image-level cosign verification, dual-approval primitives, WORM-style audit checkpoints, and Kubernetes production assets.

Production HA requires `storage.backend=postgres` with `storage.require_multi_instance_safe=true`; SQLite remains supported only for local or single-node deployments.
