from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
import os
import yaml
from pydantic import BaseModel, Field

RiskLevel = Literal["low", "medium", "high", "critical"]
PermissionMode = Literal["ask", "allow", "deny", "dry_run"]

class LLMConfig(BaseModel):
    provider: str = "openai"
    model: str = "gpt-5.1"
    api_key_env: str = "OPENAI_API_KEY"
    base_url: str | None = None
    temperature: float = 0.2
    max_input_chars: int = 12000
    max_output_tokens: int = 1200
    enable_cache: bool = True
    cache_ttl_seconds: int = 86400
    per_task_max_llm_calls: int | None = None
    require_approval_above_estimated_tokens: int = 20000


class ModelProfileConfig(BaseModel):
    enabled: bool = True
    provider: str = "openai"
    model: str = "gpt-5.1"
    api_key_env: str | None = "OPENAI_API_KEY"
    base_url: str | None = None
    api_version: str | None = None
    region: str | None = None
    temperature: float = 0.2
    max_output_tokens: int = 1200
    extra_headers: dict[str, str] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict)


class ModelsConfig(BaseModel):
    default: str = "fast"
    max_output_tokens: int = 1200
    profiles: dict[str, ModelProfileConfig] = Field(default_factory=lambda: {
        "fast": ModelProfileConfig(provider="openai", model="gpt-5.1-mini", api_key_env="OPENAI_API_KEY", max_output_tokens=800),
        "planner": ModelProfileConfig(provider="openai", model="gpt-5.1", api_key_env="OPENAI_API_KEY", max_output_tokens=1600),
        "code": ModelProfileConfig(provider="openai", model="gpt-5.1", api_key_env="OPENAI_API_KEY", max_output_tokens=4000),
        "vision": ModelProfileConfig(provider="openai", model="gpt-5.1", api_key_env="OPENAI_API_KEY", max_output_tokens=1600),
        "local": ModelProfileConfig(provider="ollama", model="qwen2.5-coder:7b", api_key_env=None, base_url="http://127.0.0.1:11434"),
    })
    routing: dict[str, str] = Field(default_factory=lambda: {
        "planner": "planner", "tool_plan": "planner", "chat": "fast", "code": "code", "upgrade": "code", "vision": "vision", "private": "local", "summarize": "fast"
    })

class GatewayConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 18789
    public_base_url: str | None = None
    shared_secret_env: str = "OMNIDESK_GATEWAY_SECRET"

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
    shell_profile: Literal["safe_ci", "upgrade"] = "safe_ci"
    shell_upgrade_enabled: bool = False

class WorkspaceConfig(BaseModel):
    root: Path = Path("~/.omnidesk/workspace").expanduser()
    skills_dirs: list[Path] = Field(default_factory=lambda: [Path("~/.omnidesk/skills").expanduser()])
    plugins_dirs: list[Path] = Field(default_factory=lambda: [Path("~/.omnidesk/plugins").expanduser()])
    memory_db: Path = Path("~/.omnidesk/memory.sqlite3").expanduser()

class PluginConfig(BaseModel):
    enabled: bool = True
    trusted_only: bool = True
    allowlist: list[str] = Field(default_factory=list)

class TelegramConfig(BaseModel):
    enabled: bool = False
    bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    webhook_secret_env: str = "TELEGRAM_WEBHOOK_SECRET"
    allowed_user_ids: list[int] = Field(default_factory=list)

class WhatsAppCloudConfig(BaseModel):
    enabled: bool = False
    access_token_env: str = "WHATSAPP_CLOUD_TOKEN"
    verify_token_env: str = "WHATSAPP_VERIFY_TOKEN"
    phone_number_id: str | None = None
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
    page_id: str | None = None
    instagram_account_id: str | None = None
    graph_version: str = "v21.0"
    verify_token_env: str = "META_VERIFY_TOKEN"
    allowed_psids: list[str] = Field(default_factory=list)

class DingTalkConfig(BaseModel):
    enabled: bool = False
    robot_access_token_env: str = "DINGTALK_ROBOT_TOKEN"
    robot_secret_env: str = "DINGTALK_ROBOT_SECRET"
    app_key_env: str = "DINGTALK_APP_KEY"
    app_secret_env: str = "DINGTALK_APP_SECRET"
    allowed_conversation_ids: list[str] = Field(default_factory=list)

class LarkConfig(BaseModel):
    enabled: bool = False
    app_id_env: str = "LARK_APP_ID"
    app_secret_env: str = "LARK_APP_SECRET"
    verification_token_env: str = "LARK_VERIFICATION_TOKEN"
    encrypt_key_env: str = "LARK_ENCRYPT_KEY"
    allowed_open_ids: list[str] = Field(default_factory=list)

class FeishuConfig(BaseModel):
    enabled: bool = False
    app_id_env: str = "FEISHU_APP_ID"
    app_secret_env: str = "FEISHU_APP_SECRET"
    verification_token_env: str = "FEISHU_VERIFICATION_TOKEN"
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
    allowed_user_ids: list[str] = Field(default_factory=list)

class GmailConfig(BaseModel):
    enabled: bool = False
    credentials_file: Path = Path("~/.omnidesk/google/credentials.json").expanduser()
    token_file: Path = Path("~/.omnidesk/google/gmail_token.json").expanduser()
    allowed_senders: list[str] = Field(default_factory=list)
    readonly: bool = True
    allow_send: bool = False
    allow_modify: bool = False
    allow_compose: bool = True
    oauth_redirect_allowlist: list[str] = Field(default_factory=list)
    oauth_state_ttl_seconds: int = 600

class ChromeConfig(BaseModel):
    enabled: bool = True
    devtools_host: str = "127.0.0.1"
    devtools_port: int = 9222
    allowed_origins: list[str] = Field(default_factory=list)
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

class UIBridgeConfig(BaseModel):
    enabled: bool = True
    require_foreground_confirmation: bool = True
    allowed_apps: list[str] = Field(default_factory=lambda: [
        "WhatsApp", "WhatsApp Business", "WeChat", "DingTalk", "Lark", "Feishu",
        "Xiaohongshu", "LINE", "X", "Telegram", "Facebook", "Instagram",
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
    gmail: GmailConfig = Field(default_factory=GmailConfig)
    chrome: ChromeConfig = Field(default_factory=ChromeConfig)
    ui_bridge: UIBridgeConfig = Field(default_factory=UIBridgeConfig)

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
    plugins: PluginConfig = Field(default_factory=PluginConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)

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

def load_config(path: str | Path | None = None) -> AppConfig:
    data: dict[str, Any] = {}
    if path:
        p = Path(path).expanduser()
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
    cfg = AppConfig.model_validate(data)
    cfg.ensure_dirs()
    return cfg

def getenv_required(env_name: str) -> str:
    value = os.getenv(env_name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {env_name}")
    return value
