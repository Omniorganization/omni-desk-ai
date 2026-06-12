from __future__ import annotations
from omnidesk_agent.config import ChromeConfig
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
