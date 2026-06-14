from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse

try:
    import httpx
except ModuleNotFoundError:
    httpx = None

from omnidesk_agent.config import ChromeConfig
from omnidesk_agent.core.models import RiskLevel, ToolResult
from omnidesk_agent.tools.base import ToolContext, proposal


def _require_httpx():
    if httpx is None:
        raise RuntimeError("httpx is required for Chrome DevTools browser control. Install with: python3 -m pip install httpx")
    return httpx


def _signature_payload(marker: dict[str, Any]) -> bytes:
    unsigned = {k: v for k, v in marker.items() if k != "signature"}
    return json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class BrowserTool:
    name = "browser"

    def __init__(self, cfg: ChromeConfig):
        self.cfg = cfg
        self.base = f"http://{cfg.devtools_host}:{cfg.devtools_port}"
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._tab_actors: dict[str, str] = {}

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    def _require_enabled(self) -> None:
        if not self.cfg.enabled:
            raise PermissionError("Browser control is disabled by configuration")
        self._verify_managed_profile_attestation()

    def _profile_attestation_path(self) -> Optional[Path]:
        profile_dir = getattr(self.cfg, "dedicated_profile_dir", None)
        if not profile_dir:
            return None
        return Path(profile_dir).expanduser() / ".omnidesk_chrome_profile_attestation.json"

    def _verify_managed_profile_attestation(self) -> None:
        if not getattr(self.cfg, "forbid_default_profile", True):
            return
        profile_dir = getattr(self.cfg, "dedicated_profile_dir", None)
        env = os.getenv("OMNIDESK_ENV", "").lower()
        strict_runtime = env in {"production", "prod", "live"}
        if not profile_dir:
            if strict_runtime:
                raise PermissionError("Browser control requires channels.chrome.dedicated_profile_dir when forbid_default_profile is true in production")
            return
        profile = Path(profile_dir).expanduser().resolve()
        marker = self._profile_attestation_path()
        if marker is None or not marker.exists():
            raise PermissionError("Managed Chrome profile attestation is missing; launch Chrome with scripts/launch_managed_chrome.py")
        self._verify_profile_permissions(profile, marker)
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except Exception as exc:
            raise PermissionError("Managed Chrome profile attestation is unreadable") from exc
        expected_hash = hashlib.sha256(str(profile).encode("utf-8")).hexdigest()
        if data.get("purpose") != "omnidesk-managed-chrome-profile" or data.get("profile_dir_sha256") != expected_hash:
            raise PermissionError("Managed Chrome profile attestation does not match channels.chrome.dedicated_profile_dir")
        if str(data.get("devtools_host") or self.cfg.devtools_host) != str(self.cfg.devtools_host):
            raise PermissionError("Managed Chrome profile attestation host mismatch")
        devtools_port = data.get("devtools_port")
        if devtools_port is None:
            raise PermissionError("Managed Chrome profile attestation port is missing")
        if int(devtools_port) != int(self.cfg.devtools_port):
            raise PermissionError("Managed Chrome profile attestation port mismatch")
        secret = os.getenv(getattr(self.cfg, "launcher_secret_env", "OMNIDESK_CHROME_LAUNCHER_SECRET"), "")
        if strict_runtime and not secret:
            raise PermissionError("Managed Chrome profile attestation requires OMNIDESK_CHROME_LAUNCHER_SECRET in production")
        if secret:
            self._verify_attestation_signature(data, secret)
        if int(data.get("schema_version") or 1) >= 2:
            self._verify_attested_browser_process(data, profile)

    @staticmethod
    def _verify_profile_permissions(profile: Path, marker: Path) -> None:
        profile_mode = profile.stat().st_mode
        if profile_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError("Managed Chrome profile directory must not be group/world writable")
        marker_mode = marker.stat().st_mode
        if marker_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError("Managed Chrome profile attestation marker must be private")

    @staticmethod
    def _verify_attestation_signature(data: dict[str, Any], secret: str) -> None:
        signature = str(data.get("signature") or "")
        if not signature.startswith("sha256="):
            raise PermissionError("Managed Chrome profile attestation signature is missing")
        expected = hmac.new(secret.encode("utf-8"), _signature_payload(data), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature.removeprefix("sha256="), expected):
            raise PermissionError("Managed Chrome profile attestation signature mismatch")

    @staticmethod
    def _process_commandline(pid: int) -> str:
        proc_cmdline = Path("/proc") / str(pid) / "cmdline"
        if proc_cmdline.exists():
            return proc_cmdline.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace")
        result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], text=True, capture_output=True, timeout=2, check=False)
        return result.stdout.strip()

    def _verify_attested_browser_process(self, data: dict[str, Any], profile: Path) -> None:
        browser_pid_raw = data.get("browser_pid")
        if browser_pid_raw is None:
            raise PermissionError("Managed Chrome profile attestation browser_pid is missing")
        try:
            browser_pid = int(browser_pid_raw)
        except (TypeError, ValueError) as exc:
            raise PermissionError("Managed Chrome profile attestation browser_pid is missing") from exc
        try:
            os.kill(browser_pid, 0)
        except OSError as exc:
            raise PermissionError("Managed Chrome profile attestation browser process is not running") from exc
        commandline = self._process_commandline(browser_pid)
        user_data_arg = f"--user-data-dir={profile}"
        if user_data_arg not in commandline:
            raise PermissionError("Managed Chrome process is not using the attested dedicated profile")
        if "--remote-debugging-address=127.0.0.1" not in commandline:
            raise PermissionError("Managed Chrome process must bind remote debugging to 127.0.0.1")
        if f"--remote-debugging-port={int(self.cfg.devtools_port)}" not in commandline:
            raise PermissionError("Managed Chrome process remote debugging port mismatch")

    def _check_url(self, url: str) -> None:
        if not self.cfg.allowed_origins:
            raise ValueError("Browser origin allowlist is empty; configure channels.chrome.allowed_origins before enabling browser control.")
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.cfg.allowed_origins:
            raise ValueError(f"Browser origin not allowed: {origin}")

    @staticmethod
    def _origin(url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}"

    def _is_high_risk_url(self, url: str) -> bool:
        haystack = url.lower()
        return any(pattern.lower() in haystack for pattern in getattr(self.cfg, "high_risk_url_patterns", []) or [])

    def _bind_tab_actor(self, tab_id: str, actor: str) -> None:
        if not tab_id:
            return
        bound = self._tab_actors.get(tab_id)
        if bound and bound != actor:
            raise PermissionError(f"Chrome tab {tab_id} is already bound to actor {bound}")
        self._tab_actors[tab_id] = actor

    async def _browser_context(self, target_id: Optional[str], actor: str) -> dict[str, Any]:
        meta: dict[str, Any] = {"actor": actor}
        try:
            tab = await self._tab(target_id)
        except Exception:
            return meta
        tab_id = str(tab.get("id") or target_id or "")
        url = str(tab.get("url") or "")
        if url.startswith("http"):
            self._check_url(url)
        self._bind_tab_actor(tab_id, actor)
        meta.update({
            "tab_id": tab_id,
            "target_id": target_id,
            "title": str(tab.get("title") or ""),
            "url": url,
            "origin": self._origin(url),
            "high_risk": self._is_high_risk_url(url),
        })
        return meta

    def _proposal_args(self, meta: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
        payload = dict(extra)
        for key in ("actor", "tab_id", "target_id", "title", "url", "origin", "target_url", "target_origin", "high_risk"):
            value = meta.get(key)
            if value not in (None, ""):
                payload[key] = value
        return payload

    @staticmethod
    def _risk(default: RiskLevel, meta: dict[str, Any]) -> RiskLevel:
        return "critical" if meta.get("high_risk") else default

    def _check_js(self, expression: str) -> None:
        if not getattr(self.cfg, "allow_evaluate", False):
            raise PermissionError("browser.evaluate is disabled by config. Use safe actions: get_dom_text, click_selector, type_selector.")
        lowered = expression.lower()
        for pat in getattr(self.cfg, "deny_js_patterns", []) or []:
            if pat.lower() in lowered:
                raise PermissionError(f"browser.evaluate blocked by deny_js_patterns: {pat}")


    def _safe_tab_summary(self, tab: dict[str, Any]) -> dict[str, Any]:
        tab_id = tab.get("id")
        url = str(tab.get("url") or "")
        origin = self._origin(url)
        if origin and origin in set(self.cfg.allowed_origins or []):
            return {"id": tab_id, "title": tab.get("title"), "url": url, "origin": origin, "redacted": False}
        return {"id": tab_id, "origin": origin or None, "redacted": True}

    async def _tabs(self) -> list[dict[str, Any]]:
        async with _require_httpx().AsyncClient(timeout=10) as client:
            r = await client.get(f"{self.base}/json")
            r.raise_for_status()
            return r.json()

    async def _tab(self, target_id: Optional[str] = None) -> dict[str, Any]:
        tabs = await self._tabs()
        if target_id:
            for tab in tabs:
                if tab.get("id") == target_id:
                    return tab
            raise ValueError(f"Chrome tab not found: {target_id}")
        if not tabs:
            raise RuntimeError("No Chrome DevTools tabs available")
        return tabs[0]

    async def _cdp(self, method: str, params: Optional[dict[str, Any]] = None, target_id: Optional[str] = None) -> dict[str, Any]:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("Install websockets or use pip install -e '.[browser]'") from exc

        async with self._get_lock():
            tab = await self._tab(target_id)
            current_url = str(tab.get("url") or "")
            if current_url.startswith("http"):
                self._check_url(current_url)

            ws_url = tab.get("webSocketDebuggerUrl")
            if not ws_url:
                raise RuntimeError("Chrome tab has no webSocketDebuggerUrl")
            request_id = secrets.randbelow(2_000_000_000) + 1
            async with websockets.connect(ws_url, open_timeout=10, close_timeout=5) as ws:
                payload = {"id": request_id, "method": method, "params": params or {}}
                await ws.send(json.dumps(payload))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                    if msg.get("id") == request_id:
                        if "error" in msg:
                            raise RuntimeError(msg["error"])
                        return msg.get("result", {})


    async def _document_node_id(self, target_id: Optional[str] = None) -> int:
        result = await self._cdp("DOM.getDocument", {"depth": 1, "pierce": False}, target_id)
        root = result.get("root") or {}
        node_id = root.get("nodeId")
        if not node_id:
            raise RuntimeError("Chrome DOM root node is unavailable")
        return int(node_id)

    async def _query_node_id(self, selector: str, target_id: Optional[str] = None) -> int:
        root_id = await self._document_node_id(target_id)
        result = await self._cdp("DOM.querySelector", {"nodeId": root_id, "selector": selector}, target_id)
        node_id = int(result.get("nodeId") or 0)
        if node_id <= 0:
            raise ValueError(f"selector not found: {selector}")
        return node_id

    async def _node_center(self, node_id: int, target_id: Optional[str] = None) -> tuple[float, float]:
        model = await self._cdp("DOM.getBoxModel", {"nodeId": node_id}, target_id)
        content = ((model.get("model") or {}).get("content") or [])
        if len(content) < 8:
            raise RuntimeError("selector has no clickable box model")
        xs = content[0::2]
        ys = content[1::2]
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    @staticmethod
    def _html_to_text(html: str) -> str:
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()[:20000]

    def spec(self):
        from omnidesk_agent.tools.spec import ActionSpec, ToolSpec, obj_schema
        return ToolSpec(
            name=self.name,
            description="Chrome DevTools browser control tool.",
            permissions=["browser.control"],
            actions={
                "list_tabs": ActionSpec("list_tabs", "List Chrome tabs with non-allowed origins redacted", obj_schema({}, additional=False), risk="medium", side_effect=False, requires_approval=True),
                "new_tab": ActionSpec("new_tab", "Open a new tab", obj_schema({"url": {"type": "string"}}, required=["url"], additional=False), risk="high", side_effect=True, requires_approval=True),
                "navigate": ActionSpec("navigate", "Navigate current tab", obj_schema({"url": {"type": "string"}, "target_id": {"type": "string"}}, required=["url"], additional=False), risk="high", side_effect=True, requires_approval=True),
                "get_dom_text": ActionSpec("get_dom_text", "Read visible DOM text", obj_schema({"target_id": {"type": "string"}}, additional=False), risk="medium", side_effect=False, requires_approval=True),
                "click_selector": ActionSpec("click_selector", "Click CSS selector", obj_schema({"selector": {"type": "string"}, "target_id": {"type": "string"}}, required=["selector"], additional=False), risk="high", side_effect=True, requires_approval=True),
                "type_selector": ActionSpec("type_selector", "Type into CSS selector", obj_schema({"selector": {"type": "string"}, "text": {"type": "string"}, "target_id": {"type": "string"}}, required=["selector", "text"], additional=False), risk="high", side_effect=True, requires_approval=True),
                "evaluate": ActionSpec("evaluate", "Evaluate JavaScript, disabled by default", obj_schema({"expression": {"type": "string"}, "target_id": {"type": "string"}}, required=["expression"], additional=False), risk="critical", side_effect=True, requires_approval=True),
                "screenshot": ActionSpec("screenshot", "Capture Chrome page screenshot", obj_schema({"target_id": {"type": "string"}}, additional=False), risk="medium", side_effect=False, requires_approval=True),
            },
        )

    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self._require_enabled()
        if action == "list_tabs":
            meta = {"actor": ctx.actor, "allowed_origins": list(self.cfg.allowed_origins or [])}
            ctx.permissions.verify(proposal("browser", "list_tabs", meta, "medium", "列出 Chrome DevTools tabs，非允许来源脱敏", ctx))
            tabs = await self._tabs()
            compact = [self._safe_tab_summary(t) for t in tabs]
            visible = sum(1 for t in compact if not t.get("redacted"))
            return ToolResult(True, data=compact, summary=f"listed {len(tabs)} chrome tabs; {visible} allowed origins visible")

        if action == "new_tab":
            url = str(args["url"])
            self._check_url(url)
            expected = str(args.get("expected_result") or f"Open {url} in Chrome")
            meta = {"actor": ctx.actor, "target_url": url, "target_origin": self._origin(url), "high_risk": self._is_high_risk_url(url)}
            ctx.permissions.verify(proposal("browser", "new_tab", self._proposal_args(meta, {"expected_result": expected}), self._risk("high", meta), "打开 Chrome 新标签页", ctx))
            async with _require_httpx().AsyncClient(timeout=10) as client:
                r = await client.put(f"{self.base}/json/new?{quote(url, safe=':/?&=%')}")
                r.raise_for_status()
            return ToolResult(True, data=r.json(), summary=f"opened chrome tab {url}")

        if action == "navigate":
            url = str(args["url"])
            self._check_url(url)
            expected = str(args.get("expected_result") or f"Navigate Chrome tab to {url}")
            meta = await self._browser_context(args.get("target_id"), ctx.actor)
            meta.update({"target_url": url, "target_origin": self._origin(url), "high_risk": bool(meta.get("high_risk")) or self._is_high_risk_url(url)})
            ctx.permissions.verify(proposal("browser", "navigate", self._proposal_args(meta, {"expected_result": expected}), self._risk("high", meta), "导航 Chrome 标签页", ctx))
            result = await self._cdp("Page.navigate", {"url": url}, args.get("target_id"))
            return ToolResult(True, data=result, summary=f"navigated to {url}")

        if action == "evaluate":
            expression = str(args["expression"])
            self._check_js(expression)
            expected = str(args.get("expected_result") or "Evaluate JavaScript in visible Chrome tab")
            meta = await self._browser_context(args.get("target_id"), ctx.actor)
            ctx.permissions.verify(proposal("browser", "evaluate", self._proposal_args(meta, {"expression_preview": expression[:300], "expected_result": expected}), "critical", "执行高风险 Chrome JavaScript", ctx))
            result = await self._cdp("Runtime.evaluate", {"expression": expression, "returnByValue": True}, args.get("target_id"))
            return ToolResult(True, data=result, summary="evaluated javascript in chrome")

        if action == "get_dom_text":
            expected = str(args.get("expected_result") or "Read visible page text from Chrome")
            meta = await self._browser_context(args.get("target_id"), ctx.actor)
            ctx.permissions.verify(proposal("browser", "get_dom_text", self._proposal_args(meta, {"expected_result": expected}), self._risk("medium", meta), "读取当前页面文本", ctx))
            try:
                node_id = await self._query_node_id("body", args.get("target_id"))
                result = await self._cdp("DOM.getOuterHTML", {"nodeId": node_id}, args.get("target_id"))
                value = self._html_to_text(str(result.get("outerHTML", "")))
                method = "DOM.getOuterHTML"
            except Exception:
                # Compatibility fallback for older/fake CDP targets. Production safe actions use
                # DOM/Input first and only fall back when explicit browser.evaluate is enabled.
                self._check_js("document.body.innerText")
                result = await self._cdp("Runtime.evaluate", {"expression": "document.body ? document.body.innerText.slice(0, 20000) : ''", "returnByValue": True}, args.get("target_id"))
                value = result.get("result", {}).get("value", "")
                method = "Runtime.evaluate.fallback"
            return ToolResult(True, data={"text": value, "method": method}, summary=f"read {len(value)} page text chars")

        if action == "click_selector":
            selector = str(args["selector"])
            expected = str(args.get("expected_result") or f"Click selector {selector}")
            meta = await self._browser_context(args.get("target_id"), ctx.actor)
            ctx.permissions.verify(proposal("browser", "click_selector", self._proposal_args(meta, {"selector": selector, "expected_result": expected}), self._risk("high", meta), "点击网页选择器", ctx))
            try:
                node_id = await self._query_node_id(selector, args.get("target_id"))
                x, y = await self._node_center(node_id, args.get("target_id"))
                await self._cdp("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1}, args.get("target_id"))
                await self._cdp("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1}, args.get("target_id"))
                data = {"selector": selector, "x": x, "y": y, "method": "DOM/Input"}
            except Exception:
                self._check_js("document.querySelector")
                js = f"document.querySelector({json.dumps(selector)})?.click(); 'clicked';"
                data = await self._cdp("Runtime.evaluate", {"expression": js, "returnByValue": True}, args.get("target_id"))
                data["method"] = "Runtime.evaluate.fallback"
            return ToolResult(True, data=data, summary=f"clicked selector {selector}")

        if action == "type_selector":
            selector = str(args["selector"])
            text = str(args["text"])
            expected = str(args.get("expected_result") or f"Type into selector {selector}")
            meta = await self._browser_context(args.get("target_id"), ctx.actor)
            ctx.permissions.verify(proposal("browser", "type_selector", self._proposal_args(meta, {"selector": selector, "text_preview": text[:200], "expected_result": expected}), self._risk("high", meta), "向网页选择器输入文本", ctx))
            try:
                node_id = await self._query_node_id(selector, args.get("target_id"))
                x, y = await self._node_center(node_id, args.get("target_id"))
                await self._cdp("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1}, args.get("target_id"))
                await self._cdp("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1}, args.get("target_id"))
                await self._cdp("Input.insertText", {"text": text}, args.get("target_id"))
                data = {"selector": selector, "length": len(text), "method": "DOM/Input"}
            except Exception:
                self._check_js("document.querySelector")
                js = (
                    f"const el=document.querySelector({json.dumps(selector)});"
                    f"if(el){{el.focus();el.value={json.dumps(text)};el.dispatchEvent(new Event('input',{{bubbles:true}}));}}"
                    "'typed';"
                )
                data = await self._cdp("Runtime.evaluate", {"expression": js, "returnByValue": True}, args.get("target_id"))
                data["method"] = "Runtime.evaluate.fallback"
            return ToolResult(True, data=data, summary=f"typed into selector {selector}")

        if action == "screenshot":
            expected = str(args.get("expected_result") or "Capture Chrome page screenshot")
            meta = await self._browser_context(args.get("target_id"), ctx.actor)
            ctx.permissions.verify(proposal("browser", "screenshot", self._proposal_args(meta, {"expected_result": expected}), self._risk("medium", meta), "截取 Chrome 页面", ctx))
            result = await self._cdp("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}, args.get("target_id"))
            return ToolResult(True, data={"png_base64": result.get("data", ""), "expected_result": expected}, summary="captured chrome screenshot")

        raise ValueError(f"Unsupported browser action: {action}")
