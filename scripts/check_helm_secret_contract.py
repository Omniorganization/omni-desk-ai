"""Prove the rendered Helm chart can boot with exactly the ExternalSecret keys.

Why: the shipped ExternalSecret omitted four secrets that the production
validator requires at boot, so every pod crash-looped and the migration hook
could not even read its DSN. This renders nothing itself; it takes the output
of ``helm template`` and:

1. loads the chart's production config from the rendered ConfigMap;
2. builds the pod environment from the Deployment's literal env, the
   Kubernetes service env and one strong dummy value per ExternalSecret key;
3. runs ``validate_production_config`` and fails on any issue;
4. checks the migration hook reads a DSN the secret provides;
5. negative control: removing any one of the previously missing keys must make
   the validator fail, so this check can detect the original defect.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PREVIOUSLY_MISSING = (
    "OMNIDESK_APPSYNC_POSTGRES_DSN",
    "OMNIDESK_MEMORY_ENCRYPTION_KEY",
    "OMNIDESK_PLUGIN_SIGNING_SECRET",
    "OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY",
)


def _documents(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(doc, dict)]


def _find(docs: list[dict], kind: str, name: str) -> dict:
    for doc in docs:
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
            return doc
    raise SystemExit(f"rendered chart has no {kind}/{name}")


def _placeholder(name: str) -> str:
    """A long, obviously fake value per key: the validator checks presence and
    strength (length, known placeholder words), so no real or random secret is
    needed or produced by this contract check."""
    if name.endswith("_DSN"):
        return "postgresql://contract-check@postgres.omnidesk-data.svc:5432/omnidesk"
    return f"helm-contract-check-{name.lower()}-" + "x" * 24


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rendered", required=True, type=Path, help="output of helm template")
    parser.add_argument("--external-secret", default=ROOT / "deploy" / "kubernetes" / "external-secret.yaml", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)

    from omnidesk_agent.config import AppConfig
    from omnidesk_agent.validation.production import validate_production_config

    rendered = _documents(args.rendered)
    configmap = _find(rendered, "ConfigMap", "omnidesk-config")
    deployment = _find(rendered, "Deployment", "omnidesk-agent")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    secret_refs = [item["secretRef"]["name"] for item in container.get("envFrom", []) if "secretRef" in item]

    external = [doc for doc in _documents(args.external_secret) if doc.get("kind") == "ExternalSecret"]
    by_target = {doc["spec"]["target"]["name"]: doc for doc in external}
    failures: list[str] = []
    if secret_refs != ["omnidesk-secrets"] or "omnidesk-secrets" not in by_target:
        failures.append(f"gateway envFrom {secret_refs} must reference the omnidesk-secrets ExternalSecret target")
    keys = [item["secretKey"] for item in by_target.get("omnidesk-secrets", {}).get("spec", {}).get("data", [])]

    env = {item["name"]: str(item["value"]) for item in container.get("env", []) if "value" in item}
    env.update({"KUBERNETES_SERVICE_HOST": "10.96.0.1", "KUBERNETES_SERVICE_PORT": "443"})
    env.update({key: _placeholder(key) for key in keys})
    config = yaml.safe_load(configmap["data"]["config.production.yaml"])
    cfg = AppConfig.model_validate(config)
    policy = validate_production_config(cfg, env)
    if not policy.get("ok"):
        failures.extend(f"validator: {issue}" for issue in policy.get("issues", []))

    hooks = [doc for doc in rendered if doc.get("kind") == "Job" and "migrate" in doc.get("metadata", {}).get("name", "")]
    for job in hooks:
        job_container = job["spec"]["template"]["spec"]["containers"][0]
        job_args = job_container.get("args", [])
        dsn_env = job_args[job_args.index("--dsn-env") + 1] if "--dsn-env" in job_args else "OMNIDESK_APPSYNC_POSTGRES_DSN"
        job_secrets = [item["secretRef"]["name"] for item in job_container.get("envFrom", []) if "secretRef" in item]
        if dsn_env not in keys or job_secrets != ["omnidesk-secrets"]:
            failures.append(f"migration hook needs {dsn_env} from omnidesk-secrets")
        labels = job["spec"]["template"]["metadata"].get("labels", {})
        if labels.get("app.kubernetes.io/name") == "omnidesk-agent":
            failures.append("migration pods must not carry the gateway label (Service/PDB/NetworkPolicy would select them)")

    negative: dict[str, bool] = {}
    for key in PREVIOUSLY_MISSING:
        reduced = {name: value for name, value in env.items() if name != key}
        negative[key] = not validate_production_config(cfg, reduced).get("ok")
        if not negative[key]:
            failures.append(f"negative control: the validator still passes without {key}; this check would not catch the defect")

    report = {
        "ok": not failures,
        "external_secret_keys": keys,
        "validator_ok": bool(policy.get("ok")),
        "migration_hooks": [job["metadata"]["name"] for job in hooks],
        "negative_control_detects_missing": negative,
        "failures": failures,
    }
    text = json.dumps(report, indent=2)
    print(text)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
