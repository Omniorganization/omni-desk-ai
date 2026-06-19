from __future__ import annotations

from pathlib import Path


def test_ga17_runtime_state_is_repository_factory_owned():
    daemon = Path("omnidesk_agent/daemon.py").read_text(encoding="utf-8")
    forbidden = [
        "learning_experiments.sqlite3",
        "memory.sqlite3",
        "token_budget.sqlite3",
        "model_costs.sqlite3",
        "ExperienceStore(",
        "TokenBudgetManager(",
        "ModelCostStore(",
        "ExperimentManager(",
    ]
    for snippet in forbidden:
        assert snippet not in daemon
    for snippet in [
        "repository_factory.learning_experiments()",
        "repository_factory.memory_store",
        "repository_factory.token_budget_manager",
        "repository_factory.model_cost_store",
    ]:
        assert snippet in daemon


def test_ga17_postgres_factory_exposes_all_ha_state_stores():
    pg = Path("omnidesk_agent/repositories/postgres.py").read_text(encoding="utf-8")
    state = Path("omnidesk_agent/repositories/postgres_state.py").read_text(encoding="utf-8")
    for method in ["learning_experiments", "memory_store", "token_budget_manager", "model_cost_store", "health_check"]:
        assert f"def {method}" in pg
    for klass in [
        "PostgresExperienceStore",
        "PostgresTokenBudgetManager",
        "PostgresModelCostStore",
        "PostgresExperimentManager",
    ]:
        assert f"class {klass}" in state
    for namespace in ["memory_experiences", "structured_experiences", "llm_cache", "llm_usage", "model_cost_events", "learning_experiments"]:
        assert namespace in state


def test_ga17_enterprise_dependencies_are_hash_locked_and_dockerfile_uses_only_lockfile():
    lock = Path("requirements.enterprise.lock").read_text(encoding="utf-8")
    assert "psycopg==" in lock
    assert "psycopg-binary==" in lock
    assert "--hash=sha256:" in lock
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "requirements.enterprise.lock" in dockerfile
    assert 'pip install --no-cache-dir "psycopg[binary]' not in dockerfile
    assert "psycopg[binary]>=" not in dockerfile


def test_ga17_kubernetes_ha_defaults_are_stateless_app_pods():
    values = Path("deploy/kubernetes/helm/omnidesk/values.yaml").read_text(encoding="utf-8")
    deploy = Path("deploy/kubernetes/helm/omnidesk/templates/deployment.yaml").read_text(encoding="utf-8")
    assert "replicaCount: 2" in values
    assert "backend: postgres" in values
    assert "requireMultiInstanceSafe: true" in values
    assert "persistence:\n  enabled: false" in values
    assert "if .Values.persistence.enabled" in deploy
    assert "emptyDir:" in deploy


def test_ga17_readiness_checks_runtime_state_sandbox_and_secrets():
    server = Path("omnidesk_agent/server.py").read_text(encoding="utf-8")
    for snippet in [
        "health_check",
        "runtime_state",
        "sandbox_runner_configured",
        "missing_secrets",
        "schema_version",
        "plugins",
    ]:
        assert snippet in server
