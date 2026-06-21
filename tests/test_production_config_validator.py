from __future__ import annotations

import pytest

from omnidesk_agent.config import AppConfig
from omnidesk_agent.validation.production import (
    ProductionConfigError,
    assert_production_config_safe,
    validate_production_config,
)


def test_local_config_is_not_strict_without_production_signal():
    cfg = AppConfig()

    result = validate_production_config(cfg, {})

    assert result == {"ok": True, "production": False, "issues": []}


def test_require_production_guards_env_is_production_signal():
    cfg = AppConfig()
    cfg.plugins.enabled = False
    cfg.memory_privacy.encrypt_at_rest = True

    result = validate_production_config(
        cfg,
        {
            "OMNIDESK_REQUIRE_PRODUCTION_GUARDS": "true",
            "OMNIDESK_ADMIN_TOKEN": "x" * 40,
            "OMNIDESK_GATEWAY_SECRET": "x" * 40,
            "OMNIDESK_MEMORY_ENCRYPTION_KEY": "x" * 40,
        },
    )

    assert result["production"] is True
    assert result["ok"] is True


def test_production_config_rejects_placeholder_secrets_and_local_bypass():
    cfg = AppConfig()
    cfg.gateway.public_base_url = "https://agent.example.com"
    cfg.gateway.allow_local_admin_without_token = True
    cfg.gateway.require_webhook_signatures = False

    result = validate_production_config(
        cfg,
        {
            "OMNIDESK_ADMIN_TOKEN": "change-me",
            "OMNIDESK_GATEWAY_SECRET": "change-me",
        },
    )

    assert result["ok"] is False
    assert "gateway admin token uses placeholder value: OMNIDESK_ADMIN_TOKEN" in result["issues"]
    assert "gateway shared secret uses placeholder value: OMNIDESK_GATEWAY_SECRET" in result["issues"]
    assert "gateway.allow_local_admin_without_token must be false in production" in result["issues"]
    assert "gateway.require_webhook_signatures must be true when public_base_url is configured" in result["issues"]


def test_production_config_accepts_hardened_minimal_runtime():
    cfg = AppConfig()
    cfg.plugins.enabled = False
    cfg.channels.chrome.enabled = False
    cfg.memory_privacy.encrypt_at_rest = True
    cfg.sandbox.docker_image = "python:3.11-slim@sha256:" + '66f011380d0e49ed280c789fbd08ff0d40968ee7b665575489afa95c98196ab5'

    result = validate_production_config(
        cfg,
        {
            "OMNIDESK_ENV": "production",
            "OMNIDESK_ADMIN_TOKEN": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "OMNIDESK_GATEWAY_SECRET": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "OMNIDESK_MEMORY_ENCRYPTION_KEY": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        },
    )

    assert result == {"ok": True, "production": True, "issues": []}


def test_production_config_requires_enabled_channel_secrets():
    cfg = AppConfig()
    cfg.plugins.enabled = False
    cfg.channels.chrome.enabled = False
    cfg.channels.telegram.enabled = True
    cfg.memory_privacy.encrypt_at_rest = True

    result = validate_production_config(
        cfg,
        {
            "OMNIDESK_ENV": "production",
            "OMNIDESK_ADMIN_TOKEN": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "OMNIDESK_GATEWAY_SECRET": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "OMNIDESK_MEMORY_ENCRYPTION_KEY": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        },
    )

    assert "telegram.bot_token_env is not configured: TELEGRAM_BOT_TOKEN" in result["issues"]
    assert "telegram.webhook_secret_env is not configured: TELEGRAM_WEBHOOK_SECRET" in result["issues"]


def test_production_config_requires_capability_for_enabled_high_risk_surfaces():
    cfg = AppConfig()
    cfg.plugins.enabled = False
    cfg.memory_privacy.encrypt_at_rest = True
    cfg.channels.chrome.enabled = True
    cfg.channels.chrome.allowed_origins = ["https://agent.example.com"]
    cfg.channels.ui_bridge.enabled = True

    result = validate_production_config(
        cfg,
        {
            "OMNIDESK_ENV": "production",
            "OMNIDESK_ADMIN_TOKEN": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "OMNIDESK_GATEWAY_SECRET": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "OMNIDESK_MEMORY_ENCRYPTION_KEY": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        },
    )

    assert "capabilities.browser.enabled must be true when channels.chrome.enabled is true" in result["issues"]
    assert "capabilities.ui_bridge.enabled must be true when channels.ui_bridge.enabled is true" in result["issues"]

    cfg.capabilities.browser.enabled = True
    cfg.capabilities.ui_bridge.enabled = True
    result = validate_production_config(
        cfg,
        {
            "OMNIDESK_ENV": "production",
            "OMNIDESK_ADMIN_TOKEN": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "OMNIDESK_GATEWAY_SECRET": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "OMNIDESK_MEMORY_ENCRYPTION_KEY": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        },
    )
    assert "capabilities.browser.enabled must be true when channels.chrome.enabled is true" not in result["issues"]
    assert "capabilities.ui_bridge.enabled must be true when channels.ui_bridge.enabled is true" not in result["issues"]


def test_assert_production_config_safe_raises():
    cfg = AppConfig()
    cfg.plugins.enabled = False
    cfg.channels.chrome.enabled = False

    with pytest.raises(ProductionConfigError, match="gateway admin token"):
        assert_production_config_safe(cfg, {"OMNIDESK_ENV": "production"})


def test_production_config_rejects_unsafe_sandbox_metrics_memory_and_http():
    cfg = AppConfig()
    cfg.plugins.enabled = False
    cfg.channels.chrome.enabled = False
    cfg.gateway.public_base_url = "http://agent.example.com"
    cfg.sandbox.backend = "argv"
    cfg.sandbox.docker_network = "bridge"
    cfg.observability.expose_public_metrics = True
    cfg.memory_privacy.redact_pii = False
    cfg.memory_privacy.isolate_by_actor = False
    cfg.memory_privacy.encrypt_at_rest = False
    cfg.memory_privacy.retention_days = 365

    result = validate_production_config(
        cfg,
        {
            "OMNIDESK_ADMIN_TOKEN": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "OMNIDESK_GATEWAY_SECRET": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        },
    )

    assert "gateway.public_base_url must use https in production" in result["issues"]
    assert "sandbox.backend must be docker or remote_docker in production" in result["issues"]
    assert "sandbox.docker_network must be none in production" in result["issues"]
    assert "observability.expose_public_metrics must be false in production; use /admin/metrics" in result["issues"]
    assert "memory_privacy.redact_pii must be true in production" in result["issues"]
    assert "memory_privacy.isolate_by_actor must be true in production" in result["issues"]
    assert "memory_privacy.encrypt_at_rest must be true in production" in result["issues"]
    assert "memory_privacy.retention_days must be between 1 and 90 in production" in result["issues"]


def test_production_config_requires_resource_and_model_budget_guards():
    cfg = AppConfig()
    cfg.plugins.enabled = False
    cfg.channels.chrome.enabled = False
    cfg.memory_privacy.encrypt_at_rest = True
    cfg.api_resource_guard.enabled = False
    cfg.models.budget.daily_usd_limit = None
    cfg.models.budget.monthly_usd_limit = 0
    cfg.models.budget.per_actor_daily_usd_limit = None
    cfg.models.budget.require_persistent_ledger = False
    cfg.llm.per_task_max_llm_calls = None

    result = validate_production_config(
        cfg,
        {
            "OMNIDESK_ENV": "production",
            "OMNIDESK_ADMIN_TOKEN": "x" * 40,
            "OMNIDESK_GATEWAY_SECRET": "x" * 40,
            "OMNIDESK_MEMORY_ENCRYPTION_KEY": "x" * 40,
        },
    )

    assert "api_resource_guard.enabled must be true in production" in result["issues"]
    assert "models.budget.daily_usd_limit must be a positive hard limit in production" in result["issues"]
    assert "models.budget.monthly_usd_limit must be a positive hard limit in production" in result["issues"]
    assert "models.budget.per_actor_daily_usd_limit must be a positive hard limit in production" in result["issues"]
    assert "models.budget.require_persistent_ledger must be true in production" in result["issues"]
    assert "llm.per_task_max_llm_calls must be a positive hard limit in production" in result["issues"]


def test_production_config_rejects_weak_secrets_and_placeholder_public_url():
    cfg = AppConfig()
    cfg.plugins.enabled = False
    cfg.channels.chrome.enabled = False
    cfg.gateway.public_base_url = "https://your-domain.example"
    cfg.memory_privacy.encrypt_at_rest = True

    result = validate_production_config(
        cfg,
        {
            "OMNIDESK_ADMIN_TOKEN": "adminadmin",
            "OMNIDESK_GATEWAY_SECRET": "short",
            "OMNIDESK_MEMORY_ENCRYPTION_KEY": "short",
        },
    )

    assert "gateway.public_base_url uses placeholder value" in result["issues"]
    assert "gateway admin token uses placeholder value: OMNIDESK_ADMIN_TOKEN" in result["issues"]
    assert "gateway shared secret must be at least 32 chars: OMNIDESK_GATEWAY_SECRET" in result["issues"]
    assert "memory encryption key must be at least 32 chars: OMNIDESK_MEMORY_ENCRYPTION_KEY" in result["issues"]
