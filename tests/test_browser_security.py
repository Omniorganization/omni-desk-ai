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
