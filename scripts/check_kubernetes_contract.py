#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REQUIRED_FILES = [
    "deploy/kubernetes/namespace.yaml",
    "deploy/kubernetes/networkpolicy.yaml",
    "deploy/kubernetes/podsecurity.yaml",
    "deploy/kubernetes/external-secret.yaml",
    "deploy/kubernetes/service-monitor.yaml",
    "deploy/kubernetes/helm/omnidesk/Chart.yaml",
    "deploy/kubernetes/helm/omnidesk/values.yaml",
    "deploy/kubernetes/helm/omnidesk/templates/deployment.yaml",
    "deploy/kubernetes/helm/omnidesk/templates/serviceaccount.yaml",
    "deploy/kubernetes/helm/omnidesk/templates/service.yaml",
    "deploy/kubernetes/helm/omnidesk/templates/configmap.yaml",
    "deploy/kubernetes/helm/omnidesk/templates/pdb.yaml",
    "deploy/kubernetes/helm/omnidesk/templates/hpa.yaml",
    "deploy/kubernetes/helm/omnidesk/templates/networkpolicy.yaml",
]
REQUIRED_SNIPPETS = {
    "deploy/kubernetes/networkpolicy.yaml": ["policyTypes:", "Ingress", "Egress"],
    "deploy/kubernetes/podsecurity.yaml": ["pod-security.kubernetes.io/enforce: restricted", "pod-security.kubernetes.io/audit: restricted"],
    "deploy/kubernetes/external-secret.yaml": ["ExternalSecret", "secretStoreRef"],
    "deploy/kubernetes/service-monitor.yaml": ["ServiceMonitor", "/admin/metrics"],
    "deploy/kubernetes/helm/omnidesk/templates/deployment.yaml": [
        "runAsNonRoot: true",
        "readOnlyRootFilesystem: true",
        "RuntimeDefault",
        "drop:",
        "ALL",
        "resources:",
        "startupProbe:",
        "livenessProbe:",
        "readinessProbe:",
        "terminationGracePeriodSeconds:",
        "preStop:",
        "topologySpreadConstraints:",
        "topology.kubernetes.io/zone",
        "whenUnsatisfiable: DoNotSchedule",
        "podAntiAffinity:",
        "configMap:",
    ],
    "deploy/kubernetes/helm/omnidesk/templates/service.yaml": ["kind: Service", "targetPort: http"],
    "deploy/kubernetes/helm/omnidesk/templates/pdb.yaml": ["PodDisruptionBudget", "minAvailable"],
    "deploy/kubernetes/helm/omnidesk/templates/hpa.yaml": [
        "HorizontalPodAutoscaler",
        "minReplicas",
        "maxReplicas",
        "name: cpu",
        "name: memory",
        "behavior:",
        "omnidesk_active_chat_streams",
        "omnidesk_postgres_pool_waiters",
    ],
    "deploy/kubernetes/helm/omnidesk/templates/configmap.yaml": ["storage:", "backend: postgres", "postgres_dsn_env", "require_multi_instance_safe: true"],
}
DIGEST_RE = re.compile(r"(?:sha256:|@sha256:)([a-f0-9]{64})")
BAD_DIGESTS = {"0" * 64, "f" * 64, "a" * 64, "0123456789abcdef" * 4}


def _bad_digest(digest: str) -> bool:
    return digest in BAD_DIGESTS or len(set(digest)) == 1


def _integer(text: str, key: str) -> int | None:
    match = re.search(rf"(?m)^\s*{re.escape(key)}:\s*(\d+)\s*$", text)
    return int(match.group(1)) if match else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Kubernetes/Helm production security and HA contract.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--locked-values", help="Optional release-rendered Helm values file with final app and sandbox-runner OCI digests.")
    args = parser.parse_args(argv)
    root = Path(args.root)
    issues: list[str] = []
    for rel in REQUIRED_FILES:
        if not (root / rel).exists():
            issues.append(f"missing Kubernetes asset: {rel}")
    for rel, snippets in REQUIRED_SNIPPETS.items():
        path = root / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for snippet in snippets:
            if snippet not in text:
                issues.append(f"{rel} missing snippet: {snippet}")

    values_path = root / "deploy/kubernetes/helm/omnidesk/values.yaml"
    locked_values_path = Path(args.locked_values) if args.locked_values else root / "deploy/kubernetes/helm/omnidesk/values.production.locked.yaml"
    if values_path.exists():
        text = values_path.read_text(encoding="utf-8")
        if 'digest: ""' not in text:
            issues.append("Helm source values must leave image digests empty for release injection")
        replica_count = _integer(text, "replicaCount")
        min_replicas = _integer(text, "minReplicas")
        max_replicas = _integer(text, "maxReplicas")
        pool_size = _integer(text, "postgresPoolSize")
        max_connections = _integer(text, "maxConnections")
        reserve_keys = (
            "reservedAdminConnections",
            "workerConnections",
            "migrationConnections",
            "monitoringConnections",
        )
        reserves = [_integer(text, key) for key in reserve_keys]
        if replica_count is None or replica_count < 3:
            issues.append("Helm production values must default to at least three gateway replicas")
        if min_replicas is None or min_replicas < 3:
            issues.append("HPA minReplicas must be at least three for production HA")
        if max_replicas is None or min_replicas is None or max_replicas < min_replicas:
            issues.append("HPA maxReplicas must be greater than or equal to minReplicas")
        if None in (pool_size, max_connections, *reserves):
            issues.append("Helm values must declare the complete PostgreSQL connection budget")
        else:
            assert pool_size is not None and max_connections is not None and max_replicas is not None
            reserved_total = sum(value for value in reserves if value is not None)
            gateway_budget = max_connections - reserved_total
            required_gateway_connections = max_replicas * pool_size
            if gateway_budget <= 0:
                issues.append("PostgreSQL reserved connection budget must leave capacity for gateways")
            if required_gateway_connections > gateway_budget:
                issues.append(
                    "HPA maxReplicas multiplied by postgresPoolSize exceeds the available PostgreSQL gateway connection budget"
                )
        if "backend: postgres" not in text or "requireMultiInstanceSafe: true" not in text:
            issues.append("Helm values must default HA storage to postgres with requireMultiInstanceSafe=true")
        if "persistence:" not in text or "enabled: false" not in text:
            issues.append("Helm HA values must default app pods to stateless persistence.enabled=false")
        if "replicaCount: 2" in text and "accessMode: ReadWriteOnce" in text and "persistence:\n  enabled: true" in text:
            issues.append("Helm must not combine replicaCount>=2 with enabled ReadWriteOnce app PVC")
        forbidden_domains = ["example.com", "example.invalid", "company.example", "your-domain", "localhost"]
        if any(token in text for token in forbidden_domains):
            issues.append("Helm values must not contain placeholder public URLs or localhost production endpoints")
        if "ingressNamespaceLabel: {}" in text or "ingressPodLabel: {}" in text:
            issues.append("Helm NetworkPolicy ingress selectors must be explicit and non-empty")

    deployment_path_for_digest = root / "deploy/kubernetes/helm/omnidesk/templates/deployment.yaml"
    if deployment_path_for_digest.exists():
        deployment_digest_contract = deployment_path_for_digest.read_text(encoding="utf-8")
        if 'required "image.digest is required' not in deployment_digest_contract:
            issues.append("Helm deployment must fail closed unless the release app OCI digest is injected")

    if locked_values_path.exists():
        locked_text = locked_values_path.read_text(encoding="utf-8")
        digests = DIGEST_RE.findall(locked_text)
        if len(digests) < 2:
            issues.append("locked Helm production values must pin app and sandbox-runner images by sha256 digest")
        for digest in digests:
            if _bad_digest(digest):
                issues.append("locked Helm production values contain placeholder or weak sha256 digest")

    network_text = "\n".join(
        p.read_text(encoding="utf-8")
        for p in [
            root / "deploy/kubernetes/networkpolicy.yaml",
            root / "deploy/kubernetes/helm/omnidesk/templates/networkpolicy.yaml",
        ]
        if p.exists()
    )
    if "namespaceSelector: {}" in network_text:
        issues.append("NetworkPolicy must not allow all namespaces with namespaceSelector: {}")
    if "ingress:\n    - from:\n        - podSelector: {}" in network_text:
        issues.append("NetworkPolicy must not allow broad podSelector: {} ingress")

    deployment_path = root / "deploy/kubernetes/helm/omnidesk/templates/deployment.yaml"
    if deployment_path.exists():
        deployment = deployment_path.read_text(encoding="utf-8")
        if "path: /ready" not in deployment:
            issues.append("Kubernetes probes must use /ready readiness checks")
        if "persistentVolumeClaim:" in deployment and "if .Values.persistence.enabled" not in deployment:
            issues.append("Helm production deployment must gate any app PVC behind persistence.enabled")

    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("kubernetes production contract verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
