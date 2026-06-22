from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional, Union
import os
try:
    import yaml
except ModuleNotFoundError:  # allow config models to import before optional deps are installed
    yaml = None
try:
    from pydantic import BaseModel, Field
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError("pydantic is required. Install with: python3 -m pip install pydantic") from exc

RiskLevel = Literal["low", "medium", "high", "critical"]
PermissionMode = Literal["ask", "allow", "deny", "dry_run"]
DEFAULT_SANDBOX_IMAGE = "python:3.11-slim@sha256:f9fa7f851e38bfb19c9de3afbc4b86ae7176ea7aaf94535c31df5458d5849457"

class LLMConfig(BaseModel):
    provider: str = "openai"
    model: str = "gpt-5.1"
    api_key_env: str = "OPENAI_API_KEY"
    base_url: Optional[str] = None
    temperature: float = 0.2
    max_input_chars: int = 12000
    max_output_tokens: int = 1200
    enable_cache: bool = True
    cache_ttl_seconds: int = 86400
    per_task_max_llm_calls: Optional[int] = 16
    require_approval_above_estimated_tokens: int = 20000


class ModelProfileConfig(BaseModel):
    enabled: bool = True
    provider: str = "openai"
    model: str = "gpt-5.1"
    api_key_env: Optional[str] = "OPENAI_API_KEY"
    base_url: Optional[str] = None
    api_version: Optional[str] = None
    region: Optional[str] = None
    temperature: float = 0.2
    max_output_tokens: int = 1200
    extra_headers: dict[str, str] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict)


class ModelCircuitBreakerConfig(BaseModel):
    failure_threshold: int = 5
    reset_seconds: int = 60


class ModelRouteConfig(BaseModel):
    primary: str
    fallback: list[str] = Field(default_factory=list)
    max_retries: int = 1
    circuit_breaker: ModelCircuitBreakerConfig = Field(default_factory=ModelCircuitBreakerConfig)


class ModelBudgetConfig(BaseModel):
    daily_usd_limit: Optional[float] = 500.0
    monthly_usd_limit: Optional[float] = 5000.0
    per_actor_daily_usd_limit: Optional[float] = 50.0
    on_exceed: Literal["require_approval", "fallback_local", "block"] = "require_approval"
    require_persistent_ledger: bool = True


class ModelsConfig(BaseModel):
    default: str = "fast"
    budget: ModelBudgetConfig = Field(default_factory=ModelBudgetConfig)
    max_output_tokens: int = 1200
    profiles: dict[str, ModelProfileConfig] = Field(default_factory=lambda: {
        "fast": ModelProfileConfig(provider="openai", model="gpt-5.1-mini", api_key_env="OPENAI_API_KEY", max_output_tokens=800),
        "planner": ModelProfileConfig(provider="openai", model="gpt-5.1", api_key_env="OPENAI_API_KEY", max_output_tokens=1600),
        "code": ModelProfileConfig(provider="openai", model="gpt-5.1", api_key_env="OPENAI_API_KEY", max_output_tokens=4000),
        "vision": ModelProfileConfig(provider="openai", model="gpt-5.1", api_key_env="OPENAI_API_KEY", max_output_tokens=1600),
        "local": ModelProfileConfig(provider="ollama", model="qwen2.5-coder:7b", api_key_env=None, base_url="http://127.0.0.1:11434"),
    })
    routing: dict[str, Any] = Field(default_factory=lambda: {
        "planner": {"primary": "planner", "fallback": ["fast", "local"], "max_retries": 1},
        "tool_plan": {"primary": "planner", "fallback": ["fast"], "max_retries": 1},
        "chat": {"primary": "fast", "fallback": ["local"], "max_retries": 1},
        "code": {"primary": "code", "fallback": ["planner"], "max_retries": 1},
        "upgrade": {"primary": "code", "fallback": ["planner"], "max_retries": 1},
        "vision": {"primary": "vision", "fallback": ["planner"], "max_retries": 1},
        "private": {"primary": "local", "fallback": [], "max_retries": 1},
        "summarize": {"primary": "fast", "fallback": ["local"], "max_retries": 1},
    })

class GatewayConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 18789
    public_base_url: Optional[str] = None
    shared_secret_env: str = "OMNIDESK_GATEWAY_SECRET"
    admin_token_env: str = "OMNIDESK_ADMIN_TOKEN"
    viewer_token_env: str = "OMNIDESK_VIEWER_TOKEN"
    operator_token_env: str = "OMNIDESK_OPERATOR_TOKEN"
    owner_token_env: str = "OMNIDESK_OWNER_TOKEN"
    allow_local_admin_without_token: bool = False
    admin_allowed_ips: list[str] = Field(default_factory=lambda: ["127.0.0.1", "::1", "localhost"])
    require_webhook_signatures: bool = True

class PermissionConfig(BaseModel):
    approval_mode: Literal["interactive_cli", "remote_approval", "auto_policy"] = "interactive_cli"
    default_mode: PermissionMode = "ask"
    no_tty_mode: PermissionMode = "deny"
    audit_log: Path = Path("~/.omnidesk/audit.log").expanduser()
    allow_low_risk_from: list[str] = Field(default_factory=lambda: ["local-cli"])
    always_ask_tools: list[str] = Field(default_factory=lambda: ["computer", "shell", "channels", "files", "ui_bridge", "browser", "gmail"])
    deny_shell_patterns: list[str] = Field(default_factory=lambda: [
        "rm -rf /", "mkfs", "shutdown", "reboot", ":(){", "dd if=", "chmod -R 777 /",
        "curl * | sh", "wget * | sh", "powershell -enc", "sudo rm", "reg delete",
    ])
    max_shell_seconds: int = 30
    approval_ttl_seconds: int = 600
    require_dual_approval_for_risks: list[str] = Field(default_factory=lambda: ["critical"])
    break_glass_enabled: bool = False
    audit_checkpoint_hmac_key_env: str = "OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY"
    shell_profile: Literal["safe_ci", "upgrade"] = "safe_ci"
    shell_upgrade_enabled: bool = False
    shell_backend: Literal["argv", "docker", "remote_docker"] = "argv"
    shell_docker_image: str = DEFAULT_SANDBOX_IMAGE
    shell_docker_network: str = "none"
    shell_docker_memory: str = "512m"
    shell_docker_cpus: str = "1.0"


class AppSyncConfig(BaseModel):
    backend: Literal["json", "postgres"] = "json"
    allow_websocket_query_auth: bool = False
    require_device_public_key_in_production: bool = True
    reject_predictable_device_ids_in_production: bool = True
    require_device_signed_requests_in_production: bool = True
    device_signature_max_skew_seconds: int = 300
    device_request_nonce_ttl_seconds: int = 600
    json_path: Optional[Path] = None
    postgres_dsn_env: str = "OMNIDESK_APPSYNC_POSTGRES_DSN"
    namespace: str = "default"
    require_idempotency: bool = True
    task_lease_seconds: int = 60

class WorkspaceConfig(BaseModel):
    root: Path = Path("~/.omnidesk/workspace").expanduser()
    skills_dirs: list[Path] = Field(default_factory=lambda: [Path("~/.omnidesk/skills").expanduser()])
    plugins_dirs: list[Path] = Field(default_factory=lambda: [Path("~/.omnidesk/plugins").expanduser()])
    memory_db: Path = Path("~/.omnidesk/memory.sqlite3").expanduser()

class PluginConfig(BaseModel):
    enabled: bool = True
    trusted_only: bool = True
    allowlist: list[str] = Field(default_factory=list)
    allow_in_process: bool = False
    default_sandbox: Literal["docker", "subprocess"] = "docker"
    plugin_timeout_seconds: int = 30
    production_forbid_subprocess: bool = True


class CapabilityToggleConfig(BaseModel):
    enabled: bool = False


class FileCapabilityConfig(CapabilityToggleConfig):
    enabled: bool = True
    allow_write: bool = False


class CapabilitiesConfig(BaseModel):
    files: FileCapabilityConfig = Field(default_factory=FileCapabilityConfig)
    test: CapabilityToggleConfig = Field(default_factory=lambda: CapabilityToggleConfig(enabled=True))
    vision: CapabilityToggleConfig = Field(default_factory=lambda: CapabilityToggleConfig(enabled=True))
    shell: CapabilityToggleConfig = Field(default_factory=CapabilityToggleConfig)
    computer: CapabilityToggleConfig = Field(default_factory=CapabilityToggleConfig)
    git: CapabilityToggleConfig = Field(default_factory=CapabilityToggleConfig)
    pull_request: CapabilityToggleConfig = Field(default_factory=CapabilityToggleConfig)
    browser: CapabilityToggleConfig = Field(default_factory=CapabilityToggleConfig)
    ui_bridge: CapabilityToggleConfig = Field(default_factory=CapabilityToggleConfig)
    gmail: CapabilityToggleConfig = Field(default_factory=CapabilityToggleConfig)
    channels: CapabilityToggleConfig = Field(default_factory=CapabilityToggleConfig)
    plugins: CapabilityToggleConfig = Field(default_factory=CapabilityToggleConfig)


class TelegramConfig(BaseModel):
    enabled: bool = False
    bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    webhook_secret_env: str = "TELEGRAM_WEBHOOK_SECRET"
    allowed_user_ids: list[int] = Field(default_factory=list)

class WhatsAppCloudConfig(BaseModel):
    enabled: bool = False
    access_token_env: str = "WHATSAPP_CLOUD_TOKEN"
    verify_token_env: str = "WHATSAPP_VERIFY_TOKEN"
    app_secret_env: str = "WHATSAPP_APP_SECRET"
    phone_number_id: Optional[str] = None
    graph_version: str = "v21.0"
    allowed_wa_ids: list[str] = Field(default_factory=list)

class WeChatOfficialConfig(BaseModel):
    enabled: bool = False
    app_id_env: str = "WECHAT_APP_ID"
    app_secret_env: str = "WECHAT_APP_SECRET"
    token_env: str = "WECHAT_TOKEN"
    encoding_aes_key_env: str = "WECHAT_ENCODING_AES_KEY"
    allowed_open_ids: list[str] = Field(default_factory=list)

class MetaGraphConfig(BaseModel):
    enabled: bool = False
    page_access_token_env: str = "META_PAGE_ACCESS_TOKEN"
    page_id: Optional[str] = None
    instagram_account_id: Optional[str] = None
    graph_version: str = "v21.0"
    verify_token_env: str = "META_VERIFY_TOKEN"
    app_secret_env: str = "META_APP_SECRET"
    allowed_psids: list[str] = Field(default_factory=list)

class DingTalkConfig(BaseModel):
    enabled: bool = False
    robot_access_token_env: str = "DINGTALK_ROBOT_TOKEN"
    robot_secret_env: str = "DINGTALK_ROBOT_SECRET"
    webhook_secret_env: str = "DINGTALK_WEBHOOK_SECRET"
    app_key_env: str = "DINGTALK_APP_KEY"
    app_secret_env: str = "DINGTALK_APP_SECRET"
    allowed_conversation_ids: list[str] = Field(default_factory=list)

class LarkConfig(BaseModel):
    enabled: bool = False
    app_id_env: str = "LARK_APP_ID"
    app_secret_env: str = "LARK_APP_SECRET"
    verification_token_env: str = "LARK_VERIFICATION_TOKEN"
    webhook_secret_env: str = "LARK_WEBHOOK_SECRET"
    encrypt_key_env: str = "LARK_ENCRYPT_KEY"
    allowed_open_ids: list[str] = Field(default_factory=list)

class FeishuConfig(BaseModel):
    enabled: bool = False
    app_id_env: str = "FEISHU_APP_ID"
    app_secret_env: str = "FEISHU_APP_SECRET"
    verification_token_env: str = "FEISHU_VERIFICATION_TOKEN"
    webhook_secret_env: str = "FEISHU_WEBHOOK_SECRET"
    encrypt_key_env: str = "FEISHU_ENCRYPT_KEY"
    allowed_open_ids: list[str] = Field(default_factory=list)

class LineConfig(BaseModel):
    enabled: bool = False
    channel_access_token_env: str = "LINE_CHANNEL_ACCESS_TOKEN"
    channel_secret_env: str = "LINE_CHANNEL_SECRET"
    allowed_user_ids: list[str] = Field(default_factory=list)

class XConfig(BaseModel):
    enabled: bool = False
    bearer_token_env: str = "X_BEARER_TOKEN"
    api_key_env: str = "X_API_KEY"
    api_secret_env: str = "X_API_SECRET"
    access_token_env: str = "X_ACCESS_TOKEN"
    access_secret_env: str = "X_ACCESS_SECRET"
    webhook_crc_token_env: str = "X_WEBHOOK_CRC_TOKEN"
    webhook_secret_env: str = "X_WEBHOOK_SECRET"
    allowed_user_ids: list[str] = Field(default_factory=list)

class SlackConfig(BaseModel):
    enabled: bool = False
    bot_token_env: str = "SLACK_BOT_TOKEN"
    signing_secret_env: str = "SLACK_SIGNING_SECRET"
    allowed_user_ids: list[str] = Field(default_factory=list)
    allowed_channel_ids: list[str] = Field(default_factory=list)
    allow_bot_messages: bool = False
    max_timestamp_skew_seconds: int = 300


class DiscordConfig(BaseModel):
    enabled: bool = False
    bot_token_env: str = "DISCORD_BOT_TOKEN"
    public_key_env: str = "DISCORD_PUBLIC_KEY"
    webhook_secret_env: str = "DISCORD_WEBHOOK_SECRET"
    allowed_user_ids: list[str] = Field(default_factory=list)
    allowed_channel_ids: list[str] = Field(default_factory=list)


class GoogleChatConfig(BaseModel):
    enabled: bool = False
    incoming_webhook_url_env: str = "GOOGLE_CHAT_WEBHOOK_URL"
    channel_token_env: str = "GOOGLE_CHAT_CHANNEL_TOKEN"
    webhook_secret_env: str = "GOOGLE_CHAT_WEBHOOK_SECRET"
    allowed_user_names: list[str] = Field(default_factory=list)
    allowed_space_names: list[str] = Field(default_factory=list)


class SignalConfig(BaseModel):
    enabled: bool = False
    rest_url: Optional[str] = None
    rest_token_env: str = "SIGNAL_REST_TOKEN"
    account_number_env: str = "SIGNAL_ACCOUNT_NUMBER"
    account_number: Optional[str] = None
    webhook_secret_env: str = "SIGNAL_WEBHOOK_SECRET"
    allowed_senders: list[str] = Field(default_factory=list)


class IMessageConfig(BaseModel):
    enabled: bool = False
    relay_url: Optional[str] = None
    relay_token_env: str = "IMESSAGE_RELAY_TOKEN"
    webhook_secret_env: str = "IMESSAGE_WEBHOOK_SECRET"
    allowed_handles: list[str] = Field(default_factory=list)
    require_foreground_confirmation: bool = True


class MicrosoftTeamsConfig(BaseModel):
    enabled: bool = False
    bot_token_env: str = "MICROSOFT_TEAMS_BOT_TOKEN"
    inbound_bearer_token_env: str = "MICROSOFT_TEAMS_INBOUND_TOKEN"
    webhook_secret_env: str = "MICROSOFT_TEAMS_WEBHOOK_SECRET"
    service_url: Optional[str] = None
    allowed_user_ids: list[str] = Field(default_factory=list)
    allowed_conversation_ids: list[str] = Field(default_factory=list)


class MatrixConfig(BaseModel):
    enabled: bool = False
    homeserver_url: Optional[str] = None
    access_token_env: str = "MATRIX_ACCESS_TOKEN"
    webhook_token_env: str = "MATRIX_WEBHOOK_TOKEN"
    webhook_secret_env: str = "MATRIX_WEBHOOK_SECRET"
    allowed_user_ids: list[str] = Field(default_factory=list)
    allowed_room_ids: list[str] = Field(default_factory=list)


class QQConfig(BaseModel):
    enabled: bool = False
    bot_app_id_env: str = "QQ_BOT_APP_ID"
    bot_token_env: str = "QQ_BOT_TOKEN"
    webhook_secret_env: str = "QQ_WEBHOOK_SECRET"
    api_base: str = "https://api.sgroup.qq.com"
    allowed_user_ids: list[str] = Field(default_factory=list)
    allowed_source_ids: list[str] = Field(default_factory=list)


class GmailConfig(BaseModel):
    enabled: bool = False
    credentials_file: Path = Path("~/.omnidesk/google/credentials.json").expanduser()
    token_file: Path = Path("~/.omnidesk/google/gmail_token.json").expanduser()
    allowed_senders: list[str] = Field(default_factory=list)
    readonly: bool = True
    allow_send: bool = False
    allow_modify: bool = False
    allow_compose: bool = False
    oauth_redirect_allowlist: list[str] = Field(default_factory=list)
    oauth_state_ttl_seconds: int = 600
    encrypt_token_at_rest: bool = True
    token_encryption_key_env: str = "OMNIDESK_GMAIL_TOKEN_ENCRYPTION_KEY"

class ChromeConfig(BaseModel):
    enabled: bool = False
    devtools_host: str = "127.0.0.1"
    devtools_port: int = 9222
    allowed_origins: list[str] = Field(default_factory=list)
    dedicated_profile_dir: Optional[Path] = None
    forbid_default_profile: bool = True
    launcher_secret_env: str = "OMNIDESK_CHROME_LAUNCHER_SECRET"
    allow_evaluate: bool = False
    deny_js_patterns: list[str] = Field(default_factory=lambda: [
        "document.cookie",
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "fetch(",
        "XMLHttpRequest",
        "navigator.sendBeacon",
    ])
    high_risk_url_patterns: list[str] = Field(default_factory=lambda: [
        "bank",
        "billing",
        "checkout",
        "payment",
        "payments",
        "adsmanager",
        "business.facebook.com",
        "admin",
        "console",
    ])

class UIBridgeConfig(BaseModel):
    enabled: bool = False
    require_foreground_confirmation: bool = True
    allowed_apps: list[str] = Field(default_factory=lambda: [
        "WhatsApp", "WhatsApp Business", "WeChat", "DingTalk", "Lark", "Feishu",
        "Slack", "Discord", "Google Chat", "Signal", "Messages", "Microsoft Teams",
        "Matrix", "QQ", "Xiaohongshu", "LINE", "X", "Telegram", "Facebook", "Instagram",
        "Google Chrome", "Gmail", "Chrome", "Safari", "Edge",
    ])

class ChannelsConfig(BaseModel):
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    whatsapp_cloud: WhatsAppCloudConfig = Field(default_factory=WhatsAppCloudConfig)
    wechat_official: WeChatOfficialConfig = Field(default_factory=WeChatOfficialConfig)
    meta_graph: MetaGraphConfig = Field(default_factory=MetaGraphConfig)
    dingtalk: DingTalkConfig = Field(default_factory=DingTalkConfig)
    lark: LarkConfig = Field(default_factory=LarkConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    line: LineConfig = Field(default_factory=LineConfig)
    x: XConfig = Field(default_factory=XConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    google_chat: GoogleChatConfig = Field(default_factory=GoogleChatConfig)
    signal: SignalConfig = Field(default_factory=SignalConfig)
    imessage: IMessageConfig = Field(default_factory=IMessageConfig)
    microsoft_teams: MicrosoftTeamsConfig = Field(default_factory=MicrosoftTeamsConfig)
    matrix: MatrixConfig = Field(default_factory=MatrixConfig)
    qq: QQConfig = Field(default_factory=QQConfig)
    gmail: GmailConfig = Field(default_factory=GmailConfig)
    chrome: ChromeConfig = Field(default_factory=ChromeConfig)
    ui_bridge: UIBridgeConfig = Field(default_factory=UIBridgeConfig)


class ObservabilityConfig(BaseModel):
    request_id_header: str = "x-request-id"
    expose_public_metrics: bool = False
    structured_json_logs: bool = True
    otlp_endpoint_env: str = "OMNIDESK_OTLP_ENDPOINT"
    otlp_timeout_seconds: float = 2.0
    trace_http_requests: bool = True


class ApiResourceGuardConfig(BaseModel):
    enabled: bool = True
    backend: Literal["memory", "sqlite", "postgres"] = "memory"
    sqlite_path: Path = Path("~/.omnidesk/api_resource_guard.sqlite3").expanduser()
    postgres_dsn_env: str = "OMNIDESK_POSTGRES_DSN"
    trusted_proxy_ips: list[str] = Field(default_factory=list)
    window_seconds: int = 60
    max_body_bytes: int = 1_048_576
    max_requests_per_ip: int = 300
    max_requests_per_endpoint: int = 120
    max_requests_per_actor: int = 120
    max_requests_per_role: int = 600
    max_requests_per_org_endpoint: int = 600
    agent_run_max_requests_per_actor: int = 20
    chat_max_requests_per_actor: int = 60
    max_inflight_requests: int = 64
    max_inflight_agent_runs: int = 4
    max_inflight_chat_requests: int = 8


class StorageConfig(BaseModel):
    backend: Literal["sqlite", "postgres"] = "sqlite"
    postgres_dsn_env: str = "OMNIDESK_POSTGRES_DSN"
    require_multi_instance_safe: bool = False


class MemoryPrivacyConfig(BaseModel):
    redact_pii: bool = True
    retention_days: int = 30
    isolate_by_actor: bool = True
    encrypt_at_rest: bool = False
    encryption_backend: Literal["local_fernet"] = "local_fernet"
    encryption_key_env: str = "OMNIDESK_MEMORY_ENCRYPTION_KEY"
    encryption_key_id: str = "default"


class SandboxConfig(BaseModel):
    backend: Literal["argv", "docker", "remote_docker"] = "docker"
    docker_image: str = DEFAULT_SANDBOX_IMAGE
    require_pinned_image: bool = False
    runner_url: Optional[str] = None
    runner_token_env: str = "OMNIDESK_SANDBOX_RUNNER_TOKEN"
    runner_hmac_secret_env: str = "OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET"
    forbid_local_docker_in_container: bool = True
    docker_network: Literal["none", "bridge"] = "none"
    timeout_seconds: int = 120
    memory_limit: str = "512m"
    cpus: str = "1.0"
    user: str = "65534:65534"
    pids_limit: int = 128
    cap_drop: list[str] = Field(default_factory=lambda: ["ALL"])
    security_opt: list[str] = Field(default_factory=lambda: ["no-new-privileges"])
    tmpfs: str = "/tmp:rw,noexec,nosuid,size=64m"  # nosec B108
    init: bool = True
    log_driver: str = "none"
    pull_policy: Literal["never", "missing", "always"] = "never"


class LearningConfig(BaseModel):
    enabled: bool = True
    daily_report_days: int = 7
    growth_plan_file: Path = Path("~/.omnidesk/growth_plan.json").expanduser()
    max_recent_failures: int = 50


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    permissions: PermissionConfig = Field(default_factory=PermissionConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    app_sync: AppSyncConfig = Field(default_factory=AppSyncConfig)
    plugins: PluginConfig = Field(default_factory=PluginConfig)
    capabilities: CapabilitiesConfig = Field(default_factory=CapabilitiesConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    api_resource_guard: ApiResourceGuardConfig = Field(default_factory=ApiResourceGuardConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    memory_privacy: MemoryPrivacyConfig = Field(default_factory=MemoryPrivacyConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)

    def ensure_dirs(self) -> None:
        self.workspace.root.mkdir(parents=True, exist_ok=True)
        self.workspace.memory_db.parent.mkdir(parents=True, exist_ok=True)
        self.permissions.audit_log.parent.mkdir(parents=True, exist_ok=True)
        for d in self.workspace.skills_dirs:
            d.mkdir(parents=True, exist_ok=True)
        for d in self.workspace.plugins_dirs:
            d.mkdir(parents=True, exist_ok=True)
        self.channels.gmail.credentials_file.parent.mkdir(parents=True, exist_ok=True)
        self.channels.gmail.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.learning.growth_plan_file.parent.mkdir(parents=True, exist_ok=True)

def deep_update(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    for k, v in update.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_update(base[k], v)
        else:
            base[k] = v
    return base


def _safe_yaml_load(stream_or_text):
    """Load YAML config when PyYAML is installed.

    Importing config models does not require PyYAML. Loading a YAML file does.
    """
    if yaml is None:
        raise RuntimeError("PyYAML is required to load YAML config files. Install with: python3 -m pip install PyYAML")
    loaded = yaml.safe_load(stream_or_text)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("OmniDesk config root must be a mapping/object")
    return loaded


def load_config(path: Optional[Union[str, Path]] = None) -> AppConfig:
    data: dict[str, Any] = {}
    if path:
        p = Path(path).expanduser()
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                data = _safe_yaml_load(f) or {}
    cfg = AppConfig.model_validate(data)
    cfg.ensure_dirs()
    return cfg

def getenv_required(env_name: str) -> str:
    value = os.getenv(env_name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {env_name}")
    return value
