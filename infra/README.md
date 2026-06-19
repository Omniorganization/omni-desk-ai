# Infrastructure

`infra/` is the stable industrial entrypoint for deployment domains. The concrete deployment files currently live in `deploy/` and are mapped here to keep the root monorepo shape clear without duplicating operational YAML.

| Infra Boundary | Source Assets |
| --- | --- |
| `docker` | `deploy/docker`, `Dockerfile`, `deploy/sandbox-runner` |
| `k8s` | `deploy/kubernetes`, `deploy/kubernetes/helm/omnidesk` |
| `otel` | `deploy/observability`, `omnidesk_agent/observability_*.py` |
