from __future__ import annotations

import os
import ipaddress
import re
from typing import Mapping, Optional

from omnidesk_agent.config import AppConfig


PLACEHOLDER_VALUES = {"change-me", "changeme", "replace-me", "replace_me", "secret", "password", "token", "abc", "123456", "test123", "adminadmin"}
PLACEHOLDER_URLS = {"https://your-domain.example", "https://example.com", "https://localhost"}
STRONG_SECRET_MIN_LENGTH = 32
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
PRODUCTION_ENV_VALUES = {"prod", "production"}
REQUIRE_PRODUCTION_GUARDS_ENV = "OMNIDESK_REQUIRE_PRODUCTION_GUARDS"
PLUGIN_SIGNING_SECRET_ENV = "OMNIDESK_PLUGIN_SIGNING_SECRET"
DIGEST_PIN_RE = re.compile(r"^[\w./:+=,@-]+@sha256:[a-f0-9]{64}$")
BAD_DIGESTS = {"0" * 64, "f" * 64, "a" * 64, "0123456789abcdef" * 4}


class ProductionConfigError(ValueError):
    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("Unsafe production configuration: " + "; ".join(issues))


def is_production_mode(cfg: AppConfig, environ: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    if _truthy(env.get(REQUIRE_PRODUCTION_GUARDS_ENV, "")):
        return True
    env_mode = (
        env.get("OMNIDESK_ENV")
        or env.get("APP_ENV")
        or env.get("ENV")
        or ""
    ).strip().lower()
    if env_mode in PRODUCTION_ENV_VALUES:
        return True
    if env.get("KUBERNETES_SERVICE_HOST"):
        return True
    if cfg.gateway.public_base_url:
        return True
    return cfg.gateway.host not in LOOPBACK_HOSTS


def validate_production_config(cfg: AppConfig, environ: Optional[Mapping[str, str]] = None) -> dict:
    env = os.environ if environ is None else environ
    production = is_production_mode(cfg, env)
    issues: list[str] = []
    if not production:
        return {"ok": True, "production": False, "issues": issues}

    _require_env(env, cfg.gateway.admin_token_env, "gateway admin token", issues, min_length=STRONG_SECRET_MIN_LENGTH)
    _require_env(env, cfg.gateway.shared_secret_env, "gateway shared secret", issues, min_length=STRONG_SECRET_MIN_LENGTH)

    if cfg.gateway.allow_local_admin_without_token:
        issues.append("gateway.allow_local_admin_without_token must be false in production")
    if cfg.gateway.public_base_url and not cfg.gateway.require_webhook_signatures:
        issues.append("gateway.require_webhook_signatures must be true when public_base_url is configured")
    if cfg.gateway.public_base_url and not str(cfg.gateway.public_base_url).startswith("https://"):
        issues.append("gateway.public_base_url must use https in production")
    if cfg.gateway.public_base_url:
        public_url = str(cfg.gateway.public_base_url).strip().lower().rstrip("/")
        if public_url in PLACEHOLDER_URLS or any(token in public_url for token in ("example.com", "example.invalid", "company.example", "your-domain", "localhost")):
            issues.append("gateway.public_base_url uses placeholder value")
    if cfg.gateway.host not in LOOPBACK_HOSTS and not cfg.gateway.public_base_url:
        issues.append("gateway.public_base_url must be configured when binding a non-loopback host in production")

    if cfg.channels.chrome.enabled:
        if not cfg.channels.chrome.allowed_origins:
            issues.append("channels.chrome.allowed_origins must be configured when browser control is enabled in production")
        if cfg.channels.chrome.devtools_host not in LOOPBACK_HOSTS:
            issues.append("channels.chrome.devtools_host must be loopback in production")
        if cfg.channels.chrome.forbid_default_profile and not cfg.channels.chrome.dedicated_profile_dir:
            issues.append("channels.chrome.dedicated_profile_dir must be configured in production; do not attach AI control to a personal Chrome profile")
    _validate_capability_registry(cfg, issues)

    if cfg.channels.gmail.allow_send and cfg.permissions.approval_mode == "auto_policy":
        issues.append("gmail.allow_send requires human approval; permissions.approval_mode cannot be auto_policy")
    if cfg.channels.gmail.allow_compose and cfg.permissions.approval_mode == "auto_policy":
        issues.append("gmail.allow_compose requires human approval; permissions.approval_mode cannot be auto_policy")
    if cfg.channels.gmail.enabled and not cfg.channels.gmail.encrypt_token_at_rest:
        issues.append("channels.gmail.encrypt_token_at_rest must be true in production when Gmail is enabled")
    if cfg.channels.gmail.enabled and cfg.channels.gmail.encrypt_token_at_rest:
        _require_env(env, cfg.channels.gmail.token_encryption_key_env, "gmail token encryption key", issues, min_length=STRONG_SECRET_MIN_LENGTH)

    if cfg.sandbox.backend not in {"docker", "remote_docker"}:
        issues.append("sandbox.backend must be docker or remote_docker in production")
    if cfg.sandbox.backend == "docker" and str(env.get("OMNIDESK_RUNNING_IN_CONTAINER", "")).lower() in {"1", "true", "yes"}:
        issues.append("sandbox.backend=docker is not allowed inside the app container; use remote_docker runner instead")
    if cfg.sandbox.backend == "remote_docker":
        if not cfg.sandbox.runner_url:
            issues.append("sandbox.runner_url must be configured when sandbox.backend=remote_docker")
        _require_env(env, cfg.sandbox.runner_token_env, "sandbox runner token", issues, min_length=STRONG_SECRET_MIN_LENGTH)
        _require_env(env, getattr(cfg.sandbox, "runner_hmac_secret_env", "OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET"), "sandbox runner HMAC secret", issues, min_length=STRONG_SECRET_MIN_LENGTH)
    if cfg.sandbox.docker_network != "none":
        issues.append("sandbox.docker_network must be none in production")
    if not _is_valid_digest_pinned_image(str(cfg.sandbox.docker_image)):
        issues.append("sandbox.docker_image must use a real sha256 digest in production")
    if getattr(cfg.permissions, "shell_backend", cfg.sandbox.backend) not in {"argv", "docker", "remote_docker"}:
        issues.append("permissions.shell_backend uses an unsupported backend")
    if getattr(cfg.permissions, "shell_backend", cfg.sandbox.backend) != "argv" and getattr(cfg.permissions, "shell_backend", cfg.sandbox.backend) != cfg.sandbox.backend:
        issues.append("permissions.shell_backend must match sandbox.backend when both are configured")

    if cfg.observability.expose_public_metrics:
        issues.append("observability.expose_public_metrics must be false in production; use /admin/metrics")

    _validate_api_resource_guard(cfg, env, issues)
    _validate_model_budget_policy(cfg, issues)

    if getattr(cfg.app_sync, "allow_websocket_query_auth", False):
        issues.append("app_sync.allow_websocket_query_auth must be false in production")
    if not getattr(cfg.app_sync, "require_device_public_key_in_production", True):
        issues.append("app_sync.require_device_public_key_in_production must be true in production")
    if not getattr(cfg.app_sync, "require_device_signed_requests_in_production", True):
        issues.append("app_sync.require_device_signed_requests_in_production must be true in production")
    if int(getattr(cfg.app_sync, "device_signature_max_skew_seconds", 0) or 0) > 300:
        issues.append("app_sync.device_signature_max_skew_seconds must be <= 300 in production")
    if not getattr(cfg.app_sync, "reject_predictable_device_ids_in_production", True):
        issues.append("app_sync.reject_predictable_device_ids_in_production must be true in production")
    if cfg.storage.require_multi_instance_safe and cfg.app_sync.backend != "postgres":
        issues.append("app_sync.backend must be postgres when storage.require_multi_instance_safe=true")
    if cfg.app_sync.backend == "postgres":
        _require_env(env, cfg.app_sync.postgres_dsn_env, "app_sync postgres dsn", issues, min_length=12)

    _validate_storage_policy(cfg, env, issues)

    if not cfg.memory_privacy.redact_pii:
        issues.append("memory_privacy.redact_pii must be true in production")
    if not cfg.memory_privacy.isolate_by_actor:
        issues.append("memory_privacy.isolate_by_actor must be true in production")
    if not cfg.memory_privacy.encrypt_at_rest:
        issues.append("memory_privacy.encrypt_at_rest must be true in production")
    else:
        if cfg.memory_privacy.encryption_backend != "local_fernet":
            issues.append("memory_privacy.encryption_backend must be local_fernet until another audited backend is implemented")
        _require_env(env, cfg.memory_privacy.encryption_key_env, "memory encryption key", issues, min_length=STRONG_SECRET_MIN_LENGTH)
    if cfg.memory_privacy.retention_days <= 0 or cfg.memory_privacy.retention_days > 90:
        issues.append("memory_privacy.retention_days must be between 1 and 90 in production")

    if cfg.plugins.enabled:
        _require_env(env, PLUGIN_SIGNING_SECRET_ENV, "plugin signing secret", issues, min_length=STRONG_SECRET_MIN_LENGTH)
        if not cfg.plugins.trusted_only:
            issues.append("plugins.trusted_only must be true in production")
        if cfg.plugins.allow_in_process:
            issues.append("plugins.allow_in_process must be false in production")
        if cfg.plugins.default_sandbox != "docker":
            issues.append("plugins.default_sandbox must be docker in production")
        if not cfg.plugins.production_forbid_subprocess:
            issues.append("plugins.production_forbid_subprocess must be true in production")

    _validate_enabled_channel_envs(cfg, env, issues)
    _validate_high_risk_approval_policy(cfg, issues)
    _validate_emergency_access_policy(cfg, env, issues)

    return {"ok": not issues, "production": True, "issues": issues}


def assert_production_config_safe(cfg: AppConfig, environ: Optional[Mapping[str, str]] = None) -> None:
    result = validate_production_config(cfg, environ)
    if not result["ok"]:
        raise ProductionConfigError(list(result["issues"]))





def _validate_storage_policy(cfg: AppConfig, env: Mapping[str, str], issues: list[str]) -> None:
    if env.get("KUBERNETES_SERVICE_HOST"):
        if cfg.storage.backend != "postgres":
            issues.append("storage.backend must be postgres when running under Kubernetes")
        if not getattr(cfg.storage, "require_multi_instance_safe", False):
            issues.append("storage.require_multi_instance_safe must be true when running under Kubernetes")
        if cfg.app_sync.backend != "postgres":
            issues.append("app_sync.backend must be postgres when running under Kubernetes")
    if getattr(cfg.storage, "require_multi_instance_safe", False) and cfg.storage.backend != "postgres":
        issues.append("storage.require_multi_instance_safe=true requires storage.backend=postgres in production")
    if cfg.storage.backend == "postgres":
        _require_env(env, cfg.storage.postgres_dsn_env, "postgres dsn", issues, min_length=12)
    if cfg.storage.backend == "sqlite" and cfg.gateway.host not in LOOPBACK_HOSTS:
        issues.append("storage.backend=sqlite is single-node only; use postgres before binding production traffic")


def _validate_api_resource_guard(cfg: AppConfig, env: Mapping[str, str], issues: list[str]) -> None:
    guard = getattr(cfg, "api_resource_guard", None)
    if guard is None or not bool(getattr(guard, "enabled", False)):
        issues.append("api_resource_guard.enabled must be true in production")
        return
    backend = str(getattr(guard, "backend", "memory") or "memory")
    if backend not in {"memory", "sqlite", "postgres"}:
        issues.append("api_resource_guard.backend must be memory, sqlite, or postgres in production")
    elif backend != "postgres":
        issues.append("api_resource_guard.backend must be postgres in production")
    if backend == "postgres":
        _require_env(env, str(getattr(guard, "postgres_dsn_env", cfg.storage.postgres_dsn_env)), "api resource guard postgres dsn", issues, min_length=12)
    _validate_trusted_proxy_ips(getattr(guard, "trusted_proxy_ips", []), issues)
    positive_fields = {
        "window_seconds": "api_resource_guard.window_seconds must be positive in production",
        "max_body_bytes": "api_resource_guard.max_body_bytes must be positive in production",
        "max_requests_per_ip": "api_resource_guard.max_requests_per_ip must be positive in production",
        "max_requests_per_endpoint": "api_resource_guard.max_requests_per_endpoint must be positive in production",
        "max_requests_per_actor": "api_resource_guard.max_requests_per_actor must be positive in production",
        "max_requests_per_role": "api_resource_guard.max_requests_per_role must be positive in production",
        "max_requests_per_org_endpoint": "api_resource_guard.max_requests_per_org_endpoint must be positive in production",
        "agent_run_max_requests_per_actor": "api_resource_guard.agent_run_max_requests_per_actor must be positive in production",
        "chat_max_requests_per_actor": "api_resource_guard.chat_max_requests_per_actor must be positive in production",
        "max_inflight_requests": "api_resource_guard.max_inflight_requests must be positive in production",
        "max_inflight_agent_runs": "api_resource_guard.max_inflight_agent_runs must be positive in production",
        "max_inflight_chat_requests": "api_resource_guard.max_inflight_chat_requests must be positive in production",
    }
    for field_name, message in positive_fields.items():
        if int(getattr(guard, field_name, 0) or 0) <= 0:
            issues.append(message)


def _validate_model_budget_policy(cfg: AppConfig, issues: list[str]) -> None:
    budget = getattr(getattr(cfg, "models", None), "budget", None)
    if budget is None:
        issues.append("models.budget must be configured in production")
        return
    for field_name in ("daily_usd_limit", "monthly_usd_limit", "per_actor_daily_usd_limit"):
        value = getattr(budget, field_name, None)
        if value is None or float(value) <= 0:
            issues.append(f"models.budget.{field_name} must be a positive hard limit in production")
    if getattr(budget, "on_exceed", "") not in {"require_approval", "fallback_local", "block"}:
        issues.append("models.budget.on_exceed must be require_approval, fallback_local, or block in production")
    if not bool(getattr(budget, "require_persistent_ledger", False)):
        issues.append("models.budget.require_persistent_ledger must be true in production")
    if cfg.storage.backend != "postgres":
        issues.append("models.budget requires storage.backend=postgres in production so the cost ledger is durable and shared")
    if getattr(cfg.llm, "per_task_max_llm_calls", None) is None or int(getattr(cfg.llm, "per_task_max_llm_calls", 0) or 0) <= 0:
        issues.append("llm.per_task_max_llm_calls must be a positive hard limit in production")


def _validate_trusted_proxy_ips(values: object, issues: list[str]) -> None:
    if values in (None, ""):
        return
    if not isinstance(values, list):
        issues.append("api_resource_guard.trusted_proxy_ips must be a list of exact IPs or CIDR ranges")
        return
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        if value in {"*", "0.0.0.0/0", "::/0"}:
            issues.append("api_resource_guard.trusted_proxy_ips must not trust all clients")
            continue
        try:
            if "/" in value:
                ipaddress.ip_network(value, strict=False)
            else:
                ipaddress.ip_address(value)
        except ValueError:
            issues.append(f"api_resource_guard.trusted_proxy_ips contains invalid proxy: {value}")

def _validate_emergency_access_policy(cfg: AppConfig, env: Mapping[str, str], issues: list[str]) -> None:
    required_dual = set(getattr(cfg.permissions, "require_dual_approval_for_risks", []))
    if "critical" not in required_dual:
        issues.append("permissions.require_dual_approval_for_risks must include critical in production")
    if getattr(cfg.permissions, "break_glass_enabled", False):
        _require_env(env, cfg.permissions.audit_checkpoint_hmac_key_env, "audit checkpoint HMAC key", issues, min_length=STRONG_SECRET_MIN_LENGTH)
        if cfg.permissions.approval_mode == "auto_policy":
            issues.append("break-glass cannot be used with permissions.approval_mode=auto_policy in production")

def _validate_enabled_channel_envs(cfg: AppConfig, env: Mapping[str, str], issues: list[str]) -> None:
    for channel_name in type(cfg.channels).model_fields:
        if channel_name in {"chrome", "ui_bridge", "gmail"}:
            continue
        channel_cfg = getattr(cfg.channels, channel_name)
        if not bool(getattr(channel_cfg, "enabled", False)):
            continue
        for field_name in type(channel_cfg).model_fields:
            if field_name.endswith("_env"):
                _require_env(env, str(getattr(channel_cfg, field_name)), f"{channel_name}.{field_name}", issues)


def _validate_high_risk_approval_policy(cfg: AppConfig, issues: list[str]) -> None:
    high_risk_tools = {"computer", "shell", "channels", "files", "ui_bridge", "browser", "gmail"}
    enabled_high_risk = {
        name
        for name in high_risk_tools
        if bool(getattr(getattr(cfg.capabilities, "browser" if name == "browser" else name), "enabled", False))
    }
    missing_enabled = sorted(enabled_high_risk - set(cfg.permissions.always_ask_tools))
    if missing_enabled:
        issues.append("permissions.always_ask_tools must include enabled high-risk capabilities: " + ", ".join(missing_enabled))
    if cfg.permissions.approval_mode != "auto_policy":
        return
    missing = sorted(high_risk_tools - set(cfg.permissions.always_ask_tools))
    if missing:
        issues.append("permissions.always_ask_tools must include high-risk tools when approval_mode=auto_policy: " + ", ".join(missing))


def _validate_capability_registry(cfg: AppConfig, issues: list[str]) -> None:
    caps = cfg.capabilities
    if cfg.channels.chrome.enabled and not caps.browser.enabled:
        issues.append("capabilities.browser.enabled must be true when channels.chrome.enabled is true")
    if cfg.channels.ui_bridge.enabled and not caps.ui_bridge.enabled:
        issues.append("capabilities.ui_bridge.enabled must be true when channels.ui_bridge.enabled is true")
    if cfg.channels.gmail.enabled and not caps.gmail.enabled:
        issues.append("capabilities.gmail.enabled must be true when channels.gmail.enabled is true")
    if cfg.plugins.enabled and not caps.plugins.enabled:
        issues.append("capabilities.plugins.enabled must be true when plugins.enabled is true")
    if _any_outbound_channel_enabled(cfg) and not caps.channels.enabled:
        issues.append("capabilities.channels.enabled must be true when outbound channels are enabled")


def _any_outbound_channel_enabled(cfg: AppConfig) -> bool:
    for name in (
        "telegram", "whatsapp_cloud", "wechat_official", "meta_graph", "dingtalk",
        "lark", "feishu", "line", "x", "slack", "discord", "google_chat",
        "signal", "imessage", "microsoft_teams", "matrix", "qq",
    ):
        if bool(getattr(getattr(cfg.channels, name), "enabled", False)):
            return True
    return bool(cfg.channels.gmail.enabled and (cfg.channels.gmail.allow_send or cfg.channels.gmail.allow_compose))


def _require_env(env: Mapping[str, str], env_name: str, label: str, issues: list[str], *, min_length: int = 0) -> None:
    value = env.get(env_name, "")
    if not value:
        issues.append(f"{label} is not configured: {env_name}")
    elif _is_placeholder(value):
        issues.append(f"{label} uses placeholder value: {env_name}")
    elif min_length and len(value.strip()) < min_length:
        issues.append(f"{label} must be at least {min_length} chars: {env_name}")


def _is_placeholder(value: str) -> bool:
    return value.strip().lower() in PLACEHOLDER_VALUES


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_valid_digest_pinned_image(image: str) -> bool:
    if not DIGEST_PIN_RE.match(image):
        return False
    digest = image.rsplit("@sha256:", 1)[1]
    if digest in BAD_DIGESTS:
        return False
    if len(set(digest)) == 1:
        return False
    return True
