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


class GatewayConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 18789
    public_base_url: str | None = None
    shared_secret_env: str = "OMNIDESK_GATEWAY_SECRET"


class PermissionConfig(BaseModel):
    default_mode: PermissionMode = "ask"
    no_tty_mode: PermissionMode = "deny"
    audit_log: Path = Path("~/.omnidesk/audit.log").expanduser()
    allow_low_risk_from: list[str] = Field(default_factory=lambda: ["local-cli"])
    always_ask_tools: list[str] = Field(default_factory=lambda: ["computer", "shell", "channels", "files"])
    deny_shell_patterns: list[str] = Field(
        default_factory=lambda: [
            "rm -rf /", "mkfs", "shutdown", "reboot", ":(){", "dd if=", "chmod -R 777 /",
            "curl * | sh", "wget * | sh", "powershell -enc", "sudo rm", "reg delete",
        ]
    )
    max_shell_seconds: int = 30


class WorkspaceConfig(BaseModel):
    root: Path = Path("~/.omnidesk/workspace").expanduser()
    skills_dirs: list[Path] = Field(default_factory=lambda: [Path("~/.omnidesk/skills").expanduser()])
    memory_db: Path = Path("~/.omnidesk/memory.sqlite3").expanduser()


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


class UIBridgeConfig(BaseModel):
    enabled: bool = True
    require_foreground_confirmation: bool = True
    allowed_apps: list[str] = Field(default_factory=lambda: ["WhatsApp", "Telegram", "WeChat", "Chrome", "Safari", "Edge"])


class ChannelsConfig(BaseModel):
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    whatsapp_cloud: WhatsAppCloudConfig = Field(default_factory=WhatsAppCloudConfig)
    wechat_official: WeChatOfficialConfig = Field(default_factory=WeChatOfficialConfig)
    meta_graph: MetaGraphConfig = Field(default_factory=MetaGraphConfig)
    ui_bridge: UIBridgeConfig = Field(default_factory=UIBridgeConfig)


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    permissions: PermissionConfig = Field(default_factory=PermissionConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)

    def ensure_dirs(self) -> None:
        self.workspace.root.mkdir(parents=True, exist_ok=True)
        self.workspace.memory_db.parent.mkdir(parents=True, exist_ok=True)
        self.permissions.audit_log.parent.mkdir(parents=True, exist_ok=True)
        for d in self.workspace.skills_dirs:
            d.mkdir(parents=True, exist_ok=True)


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
