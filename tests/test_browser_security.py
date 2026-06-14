from __future__ import annotations

import hashlib
import hmac
import json

from omnidesk_agent.config import ChromeConfig
from omnidesk_agent.tools import browser as browser_module
from omnidesk_agent.tools.browser import BrowserTool


def test_browser_evaluate_disabled_by_default():
    tool = BrowserTool(ChromeConfig())
    try:
        tool._check_js("1+1")
    except PermissionError:
        pass
    else:
        raise AssertionError("evaluate should be disabled by default")


def test_browser_blocks_cookie_js():
    tool = BrowserTool(ChromeConfig(allow_evaluate=True))
    try:
        tool._check_js("document.cookie")
    except PermissionError:
        pass
    else:
        raise AssertionError("cookie JS should be blocked")


def test_browser_empty_origin_allowlist_denies_control():
    tool = BrowserTool(ChromeConfig())
    try:
        tool._check_url("https://example.com/path")
    except ValueError as exc:
        assert "allowlist is empty" in str(exc)
    else:
        raise AssertionError("empty browser origin allowlist should deny control")


def test_browser_origin_allowlist_is_enforced():
    tool = BrowserTool(ChromeConfig(allowed_origins=["https://allowed.example"]))
    tool._check_url("https://allowed.example/path")
    try:
        tool._check_url("https://blocked.example/path")
    except ValueError as exc:
        assert "not allowed" in str(exc)
    else:
        raise AssertionError("unlisted browser origin should be blocked")

def test_browser_requires_managed_profile_attestation_when_enabled(tmp_path):
    cfg = ChromeConfig(enabled=True, dedicated_profile_dir=tmp_path, forbid_default_profile=True)
    tool = BrowserTool(cfg)
    try:
        tool._require_enabled()
    except PermissionError as exc:
        assert "attestation is missing" in str(exc)
    else:
        raise AssertionError("enabled managed browser should require profile attestation")


def test_browser_accepts_matching_managed_profile_attestation(tmp_path):
    marker = tmp_path / ".omnidesk_chrome_profile_attestation.json"
    profile = tmp_path.resolve()
    marker.write_text(json.dumps({
        "purpose": "omnidesk-managed-chrome-profile",
        "profile_dir_sha256": hashlib.sha256(str(profile).encode("utf-8")).hexdigest(),
        "devtools_host": "127.0.0.1",
        "devtools_port": 9222,
    }), encoding="utf-8")
    marker.chmod(0o600)
    cfg = ChromeConfig(enabled=True, dedicated_profile_dir=tmp_path, forbid_default_profile=True)
    BrowserTool(cfg)._require_enabled()


def test_browser_accepts_signed_v2_profile_attestation(tmp_path, monkeypatch):
    secret = "s" * 40
    monkeypatch.setenv("OMNIDESK_CHROME_LAUNCHER_SECRET", secret)
    marker = tmp_path / ".omnidesk_chrome_profile_attestation.json"
    profile = tmp_path.resolve()
    payload = {
        "schema_version": 2,
        "purpose": "omnidesk-managed-chrome-profile",
        "profile_dir": str(profile),
        "profile_dir_sha256": hashlib.sha256(str(profile).encode("utf-8")).hexdigest(),
        "devtools_host": "127.0.0.1",
        "devtools_port": 9222,
        "browser_pid": 4242,
        "launcher_pid": 4241,
        "argv": ["google-chrome", f"--user-data-dir={profile}", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=9222"],
    }
    payload["signature"] = "sha256=" + hmac.new(secret.encode("utf-8"), browser_module._signature_payload(payload), hashlib.sha256).hexdigest()
    marker.write_text(json.dumps(payload), encoding="utf-8")
    marker.chmod(0o600)
    monkeypatch.setattr(browser_module.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(
        BrowserTool,
        "_process_commandline",
        staticmethod(lambda pid: f"google-chrome --user-data-dir={profile} --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222"),
    )
    cfg = ChromeConfig(enabled=True, dedicated_profile_dir=tmp_path, forbid_default_profile=True)
    BrowserTool(cfg)._require_enabled()


def test_browser_rejects_signed_profile_attestation_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_CHROME_LAUNCHER_SECRET", "s" * 40)
    marker = tmp_path / ".omnidesk_chrome_profile_attestation.json"
    profile = tmp_path.resolve()
    marker.write_text(json.dumps({
        "schema_version": 2,
        "purpose": "omnidesk-managed-chrome-profile",
        "profile_dir_sha256": hashlib.sha256(str(profile).encode("utf-8")).hexdigest(),
        "devtools_host": "127.0.0.1",
        "devtools_port": 9222,
        "browser_pid": 4242,
        "signature": "sha256:" + "0" * 64,
    }), encoding="utf-8")
    marker.chmod(0o600)
    cfg = ChromeConfig(enabled=True, dedicated_profile_dir=tmp_path, forbid_default_profile=True)
    try:
        BrowserTool(cfg)._require_enabled()
    except PermissionError as exc:
        assert "signature" in str(exc)
    else:
        raise AssertionError("signed Browser profile attestation must reject mismatches")


def test_browser_requires_dedicated_profile_in_production(monkeypatch):
    monkeypatch.setenv("OMNIDESK_ENV", "production")
    cfg = ChromeConfig(enabled=True, forbid_default_profile=True)
    try:
        BrowserTool(cfg)._require_enabled()
    except PermissionError as exc:
        assert "dedicated_profile_dir" in str(exc)
    else:
        raise AssertionError("production browser control should require a dedicated profile")
