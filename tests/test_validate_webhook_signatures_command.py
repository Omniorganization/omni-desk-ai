"""`omnidesk validate-webhook-signatures` checks the live verification path.

The 2026-11-21 audit (runtime probe) found the command printed a hard-coded
{"ok": true}. It must now fail when signatures are not enforced, when an
enabled channel's secrets are missing, or when the guard would accept an
unsigned or wrongly signed delivery.

Review follow-up: only the secrets the channel's verification mode reads are
required, and each forged delivery is complete and wrongly signed, so it is
refused by the signature comparison and not for a missing header.
"""

from __future__ import annotations

import hmac
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from omnidesk_agent.channels import native_messaging, wechat_official
from omnidesk_agent.config import AppConfig
from omnidesk_agent.daemon import OmniDeskRuntime
from omnidesk_agent.server_routes.webhook_guard import self_test_webhook_signatures

REPO_ROOT = Path(__file__).resolve().parents[1]
TELEGRAM_ENV = {"TELEGRAM_BOT_TOKEN": "123:" + "b" * 30, "TELEGRAM_WEBHOOK_SECRET": "s" * 40}
DISCORD_PUBLIC_KEY = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def _cfg(tmp_path: Path, *, telegram: bool, enforce: bool = True) -> AppConfig:
    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "ws"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.learning.growth_plan_file = tmp_path / "growth.json"
    cfg.channels.gmail.credentials_file = tmp_path / "g" / "c.json"
    cfg.channels.gmail.token_file = tmp_path / "g" / "t.json"
    cfg.plugins.enabled = False
    cfg.channels.telegram.enabled = telegram
    cfg.channels.telegram.allowed_user_ids = [1]
    cfg.gateway.require_webhook_signatures = enforce
    cfg.ensure_dirs()
    return cfg


def _run(cfg: AppConfig, env: dict[str, str]) -> dict:
    rt = OmniDeskRuntime(cfg)
    try:
        return self_test_webhook_signatures(cfg, rt, environ=env)
    finally:
        rt.close()


def _only_channel(tmp_path: Path, monkeypatch, config_attr: str, env: dict[str, str]) -> AppConfig:
    """Enable one channel with exactly these variables set (adapters read them at startup)."""
    cfg = _cfg(tmp_path, telegram=False)
    channel = getattr(cfg.channels, config_attr)
    channel.enabled = True
    for field in type(channel).model_fields:
        if field.endswith("_env"):
            monkeypatch.delenv(getattr(channel, field), raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return cfg


def _pynacl(monkeypatch, *, accept_any: bool = False) -> None:
    """Stand in for PyNaCl, which DiscordChannel imports for Ed25519 but is not a dependency.

    VerifyKey checks the signature with `cryptography`; with accept_any it accepts every signature.
    """

    class BadSignatureError(Exception):
        pass

    class VerifyKey:
        def __init__(self, key: bytes):
            self._key = Ed25519PublicKey.from_public_bytes(key)

        def verify(self, smessage: bytes, signature: bytes) -> bytes:
            if not accept_any:
                try:
                    self._key.verify(signature, smessage)
                except InvalidSignature as exc:
                    raise BadSignatureError from exc
            return smessage

    signing, exceptions = ModuleType("nacl.signing"), ModuleType("nacl.exceptions")
    signing.VerifyKey = VerifyKey
    exceptions.BadSignatureError = BadSignatureError
    monkeypatch.setitem(sys.modules, "nacl", ModuleType("nacl"))
    monkeypatch.setitem(sys.modules, "nacl.signing", signing)
    monkeypatch.setitem(sys.modules, "nacl.exceptions", exceptions)


def test_a_configured_channel_refuses_unsigned_and_forged_deliveries(tmp_path, monkeypatch):
    for name, value in TELEGRAM_ENV.items():
        monkeypatch.setenv(name, value)

    result = _run(_cfg(tmp_path, telegram=True), TELEGRAM_ENV)

    assert result["ok"] is True
    assert result["channels"]["telegram"]["unsigned"].startswith("refused")
    assert result["channels"]["telegram"]["wrong_secret"].startswith("refused")


def test_unenforced_signatures_fail_the_check(tmp_path, monkeypatch):
    for name, value in TELEGRAM_ENV.items():
        monkeypatch.setenv(name, value)

    result = _run(_cfg(tmp_path, telegram=True, enforce=False), TELEGRAM_ENV)

    assert result["ok"] is False
    assert result["require_webhook_signatures"] is False
    assert result["channels"]["telegram"]["unsigned"] == "ACCEPTED"


def test_a_missing_secret_fails_the_check(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TELEGRAM_ENV["TELEGRAM_BOT_TOKEN"])
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)

    result = _run(_cfg(tmp_path, telegram=True), {"TELEGRAM_BOT_TOKEN": TELEGRAM_ENV["TELEGRAM_BOT_TOKEN"]})

    assert result["ok"] is False
    assert result["channels"]["telegram"]["missing_env"] == ["TELEGRAM_WEBHOOK_SECRET"]


def test_no_inbound_channel_is_reported_as_such(tmp_path):
    result = _run(_cfg(tmp_path, telegram=False), {})

    assert result == {"ok": True, "require_webhook_signatures": True, "channels": {}, "note": "no inbound channel is enabled"}


def test_cli_exit_code_follows_the_result(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "workspace": {"root": str(tmp_path / "ws"), "memory_db": str(tmp_path / "m.sqlite3"),
                      "skills_dirs": [str(tmp_path / "s")], "plugins_dirs": [str(tmp_path / "p")]},
        "permissions": {"audit_log": str(tmp_path / "audit.log")},
        "learning": {"growth_plan_file": str(tmp_path / "growth.json")},
        "plugins": {"enabled": False},
        "gateway": {"require_webhook_signatures": False},
        "channels": {"telegram": {"enabled": True, "allowed_user_ids": [1]},
                     "gmail": {"credentials_file": str(tmp_path / "c.json"), "token_file": str(tmp_path / "t.json")}},
    }))
    env = {**os.environ, **TELEGRAM_ENV, "PYTHONPATH": str(REPO_ROOT), "HOME": str(tmp_path)}

    # Same entry point as the `omnidesk` console script (pyproject: omnidesk_agent.cli:main).
    launcher = "import sys; from omnidesk_agent.cli import main; sys.argv = ['omnidesk', *sys.argv[1:]]; main()"
    failed = subprocess.run([sys.executable, "-c", launcher, "--config", str(config), "validate-webhook-signatures"],
                            capture_output=True, text=True, env=env, timeout=120)

    assert failed.returncode == 1, failed.stderr[-2000:]
    assert json.loads(failed.stdout)["ok"] is False


# (channels.<attr>, route channel, the only variables set, what is missing without them)
@pytest.mark.parametrize("config_attr, route, env, required", [
    ("wechat_official", "wechat", {"WECHAT_TOKEN": "t" * 32}, ["WECHAT_TOKEN"]),
    ("discord", "discord", {"DISCORD_PUBLIC_KEY": DISCORD_PUBLIC_KEY}, ["DISCORD_PUBLIC_KEY or DISCORD_WEBHOOK_SECRET"]),
])
def test_a_channel_needs_only_the_secrets_its_verification_mode_reads(tmp_path, monkeypatch, config_attr, route, env, required):
    # Demanding every *_env variable failed valid deployments: WeChat's plaintext mode never
    # reads WECHAT_ENCODING_AES_KEY, and Discord's Ed25519 check needs no bridge HMAC secret.
    # The secret the mode does read stays required.
    _pynacl(monkeypatch)

    passed = _run(_only_channel(tmp_path, monkeypatch, config_attr, env), env)
    failed = _run(_only_channel(tmp_path, monkeypatch, config_attr, {}), {})

    assert passed["ok"] is True, passed
    assert passed["channels"][route]["missing_env"] == []
    assert failed["ok"] is False
    assert failed["channels"][route]["missing_env"] == required


def _slack_accepts_any_signature(monkeypatch) -> None:
    monkeypatch.setattr(native_messaging, "hmac", SimpleNamespace(new=hmac.new, compare_digest=lambda _a, _b: True))


def _wechat_accepts_any_signature(monkeypatch) -> None:
    monkeypatch.setattr(wechat_official, "hmac", SimpleNamespace(compare_digest=lambda _a, _b: True))


def _discord_accepts_any_signature(monkeypatch) -> None:
    _pynacl(monkeypatch, accept_any=True)


@pytest.mark.parametrize("config_attr, route, env, accept_any_signature", [
    ("slack", "slack", {"SLACK_SIGNING_SECRET": "s" * 40}, _slack_accepts_any_signature),
    ("wechat_official", "wechat", {"WECHAT_TOKEN": "t" * 32}, _wechat_accepts_any_signature),
    ("discord", "discord", {"DISCORD_PUBLIC_KEY": DISCORD_PUBLIC_KEY}, _discord_accepts_any_signature),
])
def test_the_forged_delivery_is_refused_by_the_signature_comparison(tmp_path, monkeypatch, config_attr, route, env, accept_any_signature):
    # A forged delivery refused for a missing header or query parameter proves nothing about
    # the signature check. Each one is complete and wrongly signed, so once the adapter's
    # comparison accepts any signature the self-test must report the channel as failing.
    _pynacl(monkeypatch)
    assert _run(_only_channel(tmp_path, monkeypatch, config_attr, env), env)["ok"] is True

    accept_any_signature(monkeypatch)
    result = _run(_only_channel(tmp_path, monkeypatch, config_attr, env), env)

    assert result["ok"] is False
    assert result["channels"][route]["wrong_secret"] == "ACCEPTED"
    assert result["channels"][route]["unsigned"].startswith("refused")  # refused before any comparison
