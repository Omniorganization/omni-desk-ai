from __future__ import annotations

import pytest

from omnidesk_agent.config import AppConfig
from omnidesk_agent.validation.production import validate_production_config

ROLE_ENVS = {
    "viewer": "OMNIDESK_VIEWER_TOKEN",
    "operator": "OMNIDESK_OPERATOR_TOKEN",
    "owner": "OMNIDESK_OWNER_TOKEN",
}


def _hardened_config() -> AppConfig:
    cfg = AppConfig()
    cfg.plugins.enabled = False
    cfg.channels.chrome.enabled = False
    cfg.memory_privacy.encrypt_at_rest = True
    cfg.storage.backend = "postgres"
    cfg.app_sync.backend = "postgres"
    cfg.api_resource_guard.backend = "postgres"
    return cfg


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "OMNIDESK_ENV": "production",
        "OMNIDESK_ADMIN_TOKEN": "a" * 40,
        "OMNIDESK_GATEWAY_SECRET": "g" * 40,
        "OMNIDESK_MEMORY_ENCRYPTION_KEY": "m" * 40,
        "OMNIDESK_POSTGRES_DSN": "postgresql://user:pass@db/omnidesk",
        "OMNIDESK_APPSYNC_POSTGRES_DSN": "postgresql://user:pass@db/omnidesk",
        "OMNIDESK_APPSYNC_SECRET_PEPPER": "p" * 40,
    }
    env.update(overrides)
    return env


def _role_issues(result: dict) -> list[str]:
    return [issue for issue in result["issues"] if issue.startswith("gateway ")]


def test_distinct_strong_role_tokens_are_accepted():
    result = validate_production_config(_hardened_config(), _env(
        OMNIDESK_VIEWER_TOKEN="v" * 40,
        OMNIDESK_OPERATOR_TOKEN="o" * 40,
        OMNIDESK_OWNER_TOKEN="w" * 40,
    ))

    assert result["ok"], result["issues"]


def test_unset_role_tokens_stay_optional():
    result = validate_production_config(_hardened_config(), _env(OMNIDESK_VIEWER_TOKEN=""))

    assert result["ok"], result["issues"]


@pytest.mark.parametrize("role,env_name", sorted(ROLE_ENVS.items()))
def test_short_role_token_is_rejected(role, env_name):
    result = validate_production_config(_hardened_config(), _env(**{env_name: "s" * 31}))

    assert not result["ok"]
    assert _role_issues(result) == [f"gateway {role} token must be at least 32 chars: {env_name}"]


@pytest.mark.parametrize("value", ["change-me", "  token  ", "ADMINADMIN"])
@pytest.mark.parametrize("role,env_name", sorted(ROLE_ENVS.items()))
def test_placeholder_role_token_is_rejected(role, env_name, value):
    result = validate_production_config(_hardened_config(), _env(**{env_name: value}))

    assert not result["ok"]
    assert _role_issues(result) == [f"gateway {role} token uses placeholder value: {env_name}"]


def test_whitespace_only_role_token_is_rejected():
    result = validate_production_config(_hardened_config(), _env(OMNIDESK_OPERATOR_TOKEN=" " * 40))

    assert _role_issues(result) == ["gateway operator token must be at least 32 chars: OMNIDESK_OPERATOR_TOKEN"]


@pytest.mark.parametrize("first,second", [
    ("OMNIDESK_VIEWER_TOKEN", "OMNIDESK_OPERATOR_TOKEN"),
    ("OMNIDESK_OPERATOR_TOKEN", "OMNIDESK_OWNER_TOKEN"),
    ("OMNIDESK_VIEWER_TOKEN", "OMNIDESK_OWNER_TOKEN"),
])
def test_two_roles_sharing_one_token_are_rejected(first, second):
    shared = "s" * 40
    result = validate_production_config(_hardened_config(), _env(**{first: shared, second: shared}))

    assert not result["ok"]
    issues = _role_issues(result)
    assert len(issues) == 1
    assert issues[0].endswith(f"tokens must be different: {first}, {second}")
    assert shared not in " ".join(result["issues"])


def test_role_token_reusing_admin_token_is_rejected():
    result = validate_production_config(_hardened_config(), _env(OMNIDESK_OWNER_TOKEN="a" * 40))

    assert _role_issues(result) == ["gateway admin and owner tokens must be different: OMNIDESK_ADMIN_TOKEN, OMNIDESK_OWNER_TOKEN"]


def test_tokens_differing_only_by_surrounding_whitespace_count_as_shared():
    result = validate_production_config(_hardened_config(), _env(
        OMNIDESK_VIEWER_TOKEN="s" * 40,
        OMNIDESK_OPERATOR_TOKEN=" " + "s" * 40 + " ",
    ))

    assert _role_issues(result) == ["gateway viewer and operator tokens must be different: OMNIDESK_VIEWER_TOKEN, OMNIDESK_OPERATOR_TOKEN"]


def test_two_roles_bound_to_one_env_name_are_rejected():
    cfg = _hardened_config()
    cfg.gateway.operator_token_env = "OMNIDESK_VIEWER_TOKEN"

    result = validate_production_config(cfg, _env(OMNIDESK_VIEWER_TOKEN="v" * 40))

    assert _role_issues(result) == ["gateway viewer and operator tokens must be different: OMNIDESK_VIEWER_TOKEN, OMNIDESK_VIEWER_TOKEN"]


def test_custom_role_env_names_from_config_are_checked():
    cfg = _hardened_config()
    cfg.gateway.viewer_token_env = "CUSTOM_VIEWER_TOKEN"

    weak = validate_production_config(cfg, _env(CUSTOM_VIEWER_TOKEN="short"))
    ignored = validate_production_config(cfg, _env(OMNIDESK_VIEWER_TOKEN="short"))

    assert _role_issues(weak) == ["gateway viewer token must be at least 32 chars: CUSTOM_VIEWER_TOKEN"]
    assert ignored["ok"], ignored["issues"]


def test_role_tokens_are_not_checked_outside_production():
    env = {"OMNIDESK_VIEWER_TOKEN": "short", "OMNIDESK_OPERATOR_TOKEN": "short"}

    assert validate_production_config(AppConfig(), env) == {"ok": True, "production": False, "issues": []}
