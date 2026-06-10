from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlparse

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
        origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        if origin not in self.cfg.allowed_origins:
            raise ValueError(f"Browser origin not allowed: {origin}")

    def _check_js(self, expression: str) -> None:
        if not getattr(self.cfg, "allow_evaluate", False):
            raise PermissionError("browser.evaluate is disabled by config. Use safe actions: get_dom_text, click_selector, type_selector.")
        lowered = expression.lower()
        for pat in getattr(self.cfg, "deny_js_patterns", []) or []:
            if pat.lower() in lowered:
                raise PermissionError(f"browser.evaluate blocked by deny_js_patterns: {pat}")

    async def _tabs(self) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self.base}/json")
            r.raise_for_status()
            return r.json()

    async def _tab(self, target_id: str | None = None) -> dict[str, Any]:
        tabs = await self._tabs()
        if target_id:
            for tab in tabs:
                if tab.get("id") == target_id:
                    return tab
            raise ValueError(f"Chrome tab not found: {target_id}")
        if not tabs:
            raise RuntimeError("No Chrome DevTools tabs available")
        return tabs[0]

    async def _cdp(self, method: str, params: dict[str, Any] | None = None, target_id: str | None = None) -> dict[str, Any]:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("Install websockets or use pip install -e '.[browser]'") from exc

        tab = await self._tab(target_id)
        current_url = str(tab.get("url") or "")
        if current_url.startswith("http"):
            self._check_url(current_url)

        ws_url = tab.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("Chrome tab has no webSocketDebuggerUrl")
        async with websockets.connect(ws_url, open_timeout=10) as ws:
            payload = {"id": 1, "method": method, "params": params or {}}
            await ws.send(json.dumps(payload))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise RuntimeError(msg["error"])
                    return msg.get("result", {})

    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if action == "list_tabs":
            ctx.permissions.verify(proposal("browser", "list_tabs", {}, "low", "列出 Chrome DevTools tabs", ctx))
            tabs = await self._tabs()
            compact = [{"id": t.get("id"), "title": t.get("title"), "url": t.get("url")} for t in tabs]
            return ToolResult(True, data=compact, summary=f"listed {len(tabs)} chrome tabs")

        if action == "new_tab":
            url = str(args["url"])
            self._check_url(url)
            expected = str(args.get("expected_result") or f"Open {url} in Chrome")
            ctx.permissions.verify(proposal("browser", "new_tab", {"url": url, "expected_result": expected}, "high", "打开 Chrome 新标签页", ctx))
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.put(f"{self.base}/json/new?{quote(url, safe=':/?&=%')}")
                r.raise_for_status()
            return ToolResult(True, data=r.json(), summary=f"opened chrome tab {url}")

        if action == "navigate":
            url = str(args["url"])
            self._check_url(url)
            expected = str(args.get("expected_result") or f"Navigate Chrome tab to {url}")
            ctx.permissions.verify(proposal("browser", "navigate", {"url": url, "expected_result": expected}, "high", "导航 Chrome 标签页", ctx))
            result = await self._cdp("Page.navigate", {"url": url}, args.get("target_id"))
            return ToolResult(True, data=result, summary=f"navigated to {url}")

        if action == "evaluate":
            expression = str(args["expression"])
            self._check_js(expression)
            expected = str(args.get("expected_result") or "Evaluate JavaScript in visible Chrome tab")
            ctx.permissions.verify(proposal("browser", "evaluate", {"expression_preview": expression[:300], "expected_result": expected}, "critical", "执行高风险 Chrome JavaScript", ctx))
            result = await self._cdp("Runtime.evaluate", {"expression": expression, "returnByValue": True}, args.get("target_id"))
            return ToolResult(True, data=result, summary="evaluated javascript in chrome")

        if action == "get_dom_text":
            expected = str(args.get("expected_result") or "Read visible page text from Chrome")
            ctx.permissions.verify(proposal("browser", "get_dom_text", {"expected_result": expected}, "medium", "读取当前页面文本", ctx))
            js = "document.body ? document.body.innerText.slice(0, 20000) : ''"
            result = await self._cdp("Runtime.evaluate", {"expression": js, "returnByValue": True}, args.get("target_id"))
            value = result.get("result", {}).get("value", "")
            return ToolResult(True, data={"text": value}, summary=f"read {len(value)} page text chars")

        if action == "click_selector":
            selector = str(args["selector"])
            expected = str(args.get("expected_result") or f"Click selector {selector}")
            ctx.permissions.verify(proposal("browser", "click_selector", {"selector": selector, "expected_result": expected}, "high", "点击网页选择器", ctx))
            js = f"document.querySelector({json.dumps(selector)})?.click(); 'clicked';"
            result = await self._cdp("Runtime.evaluate", {"expression": js, "returnByValue": True}, args.get("target_id"))
            return ToolResult(True, data=result, summary=f"clicked selector {selector}")

        if action == "type_selector":
            selector = str(args["selector"])
            text = str(args["text"])
            expected = str(args.get("expected_result") or f"Type into selector {selector}")
            ctx.permissions.verify(proposal("browser", "type_selector", {"selector": selector, "text_preview": text[:200], "expected_result": expected}, "high", "向网页选择器输入文本", ctx))
            js = (
                f"const el=document.querySelector({json.dumps(selector)});"
                f"if(el){{el.focus();el.value={json.dumps(text)};el.dispatchEvent(new Event('input',{{bubbles:true}}));}}"
                "'typed';"
            )
            result = await self._cdp("Runtime.evaluate", {"expression": js, "returnByValue": True}, args.get("target_id"))
            return ToolResult(True, data=result, summary=f"typed into selector {selector}")

        if action == "screenshot":
            expected = str(args.get("expected_result") or "Capture Chrome page screenshot")
            ctx.permissions.verify(proposal("browser", "screenshot", {"expected_result": expected}, "medium", "截取 Chrome 页面", ctx))
            result = await self._cdp("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}, args.get("target_id"))
            return ToolResult(True, data={"png_base64": result.get("data", ""), "expected_result": expected}, summary="captured chrome screenshot")

        raise ValueError(f"Unsupported browser action: {action}")
