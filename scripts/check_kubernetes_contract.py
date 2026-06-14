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
        "podAntiAffinity:",
        "configMap:",
    ],
    "deploy/kubernetes/helm/omnidesk/templates/service.yaml": ["kind: Service", "targetPort: http"],
    "deploy/kubernetes/helm/omnidesk/templates/pdb.yaml": ["PodDisruptionBudget", "minAvailable"],
    "deploy/kubernetes/helm/omnidesk/templates/hpa.yaml": ["HorizontalPodAutoscaler", "minReplicas", "maxReplicas"],
    "deploy/kubernetes/helm/omnidesk/templates/configmap.yaml": ["storage:", "backend: postgres", "postgres_dsn_env", "require_multi_instance_safe: true"],
}
DIGEST_RE = re.compile(r"(?:sha256:|@sha256:)([a-f0-9]{64})")
BAD_DIGESTS = {"0" * 64, "f" * 64, "a" * 64, "0123456789abcdef" * 4}


def _bad_digest(digest: str) -> bool:
    return digest in BAD_DIGESTS or len(set(digest)) == 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Kubernetes/Helm production security and HA contract.")
    parser.add_argument("root", nargs="?", default=".")
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
    if values_path.exists():
        text = values_path.read_text(encoding="utf-8")
        digests = DIGEST_RE.findall(text)
        if len(digests) < 2:
            issues.append("Helm values must pin app and sandbox-runner images by sha256 digest")
        for digest in digests:
            if _bad_digest(digest):
                issues.append("Helm values contain placeholder or weak sha256 digest")
        if "replicaCount: 1" in text:
            issues.append("Helm production values must not default to a single replica")
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

    network_text = "\n".join(
        p.read_text(encoding="utf-8") for p in [
            root / "deploy/kubernetes/networkpolicy.yaml",
            root / "deploy/kubernetes/helm/omnidesk/templates/networkpolicy.yaml",
        ] if p.exists()
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
