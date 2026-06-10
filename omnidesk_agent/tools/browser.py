from __future__ import annotations
from typing import Any
from urllib.parse import urlparse
import httpx
from omnidesk_agent.config import ChromeConfig
from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext, proposal

class BrowserTool:
    name = "browser"
    def __init__(self, cfg: ChromeConfig):
        self.cfg = cfg
        self.base = f"http://{cfg.devtools_host}:{cfg.devtools_port}"
    def _check_url(self, url: str) -> None:
        if not self.cfg.allowed_origins:
            return
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.cfg.allowed_origins:
            raise ValueError(f"Browser origin not allowed: {origin}")
    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if action == "list_tabs":
            ctx.permissions.verify(proposal("browser", "list_tabs", {}, "low", "列出 Chrome DevTools tabs", ctx))
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{self.base}/json")
                r.raise_for_status()
                tabs = r.json()
            return ToolResult(True, data=tabs, summary=f"listed {len(tabs)} chrome tabs")
        if action == "new_tab":
            url = str(args["url"])
            self._check_url(url)
            expected = str(args.get("expected_result") or f"Open {url} in Chrome")
            ctx.permissions.verify(proposal("browser", "new_tab", {"url": url, "expected_result": expected}, "high", "打开 Chrome 新标签页", ctx))
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.put(f"{self.base}/json/new?{url}")
                r.raise_for_status()
                data = r.json()
            return ToolResult(True, data=data, summary=f"opened chrome tab {url}")
        raise ValueError(f"Unsupported browser action: {action}")
