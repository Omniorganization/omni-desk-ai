"""Which application is in the foreground, for the UI bridge's visible-target rule.

The UI bridge may act only in the app the operator named, while that app is the
visible foreground window and shows a signed-in session. These probes ask the
operating system, read-only, and return None when they cannot tell; callers
must then refuse to act (fail closed).

- macOS: `lsappinfo` (front application name and bundle id; no window title).
- Windows: the foreground window title (user32).
- Linux/X11: `xdotool` (active window title and class), when installed.
"""
from __future__ import annotations

import ctypes
import platform
import re
# Fixed argv, no shell, read-only OS queries.
import subprocess  # nosec B404
from dataclasses import dataclass
from typing import Optional

_TIMEOUT_SECONDS = 2

# Names an app shows under, beyond its allowlist name (localized names, short names).
APP_ALIASES: dict[str, tuple[str, ...]] = {
    "google chrome": ("chrome",),
    "chrome": ("google chrome",),
    "wechat": ("微信", "weixin", "xinwechat"),
    "feishu": ("飞书", "lark"),
    "lark": ("飞书", "feishu"),
    "dingtalk": ("钉钉",),
    "microsoft teams": ("teams", "msteams"),
    "messages": ("信息", "imessage", "mobilesms"),
    "xiaohongshu": ("小红书", "rednote"),
    "whatsapp business": ("whatsappbusiness",),
}

# Window titles of sign-in, QR-login and verification screens.
_SIGN_IN_RE = re.compile(
    r"\b(log ?in|sign ?in|signin|sign up|verify (your|it's)|two[- ]factor|2fa)\b|登录|登入|扫码|扫一扫|ログイン|로그인",
    re.IGNORECASE,
)
_KEY_VALUE_RE = re.compile(r'"([^"]+)"\s*=\s*"([^"]*)"')


@dataclass(frozen=True)
class ForegroundWindow:
    app: str = ""  # application name as the OS reports it
    title: str = ""  # window title, when the OS reports it
    bundle_id: str = ""  # macOS bundle identifier

    def describe(self) -> str:
        return " / ".join(part for part in (self.app, self.title, self.bundle_id) if part) or "unknown window"


def _norm(value: str) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").casefold())


def _names(app: str) -> set[str]:
    return {_norm(app)} | {_norm(alias) for alias in APP_ALIASES.get(app.strip().casefold(), ())}


def window_matches_app(window: ForegroundWindow, app: str) -> bool:
    """Whether the foreground window belongs to `app` (its name, an alias, its bundle id or its title)."""
    names = {name for name in _names(app) if name}
    if not names:
        return False
    if window.app and _norm(window.app) in names:
        return True
    bundle = _norm(window.bundle_id)
    if bundle and any(len(name) >= 3 and name in bundle for name in names):
        return True
    if window.title:
        for candidate in (app, *APP_ALIASES.get(app.strip().casefold(), ())):
            if not candidate:
                continue
            if not candidate.isascii():
                if candidate in window.title:
                    return True
            elif len(_norm(candidate)) >= 3:
                if re.search(rf"(?<!\w){re.escape(candidate)}(?!\w)", window.title, re.IGNORECASE):
                    return True
            elif window.title.strip().casefold() == candidate.casefold() or re.search(rf"[-|–] {re.escape(candidate)}$", window.title, re.IGNORECASE):
                return True  # one- or two-letter names ('X') only as the whole title or its suffix
    return False


def looks_like_sign_in(window: ForegroundWindow) -> bool:
    return bool(window.title and _SIGN_IN_RE.search(window.title))


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS, check=True).stdout  # nosec B603


def _macos() -> Optional[ForegroundWindow]:
    front = _run(["lsappinfo", "front"]).strip()
    if not front:
        return None
    pairs = dict(_KEY_VALUE_RE.findall(_run(["lsappinfo", "info", "-only", "name", "-only", "bundleid", front])))
    name = pairs.get("LSDisplayName") or pairs.get("name") or ""
    bundle = pairs.get("CFBundleIdentifier") or pairs.get("bundleid") or pairs.get("bundleID") or ""
    return ForegroundWindow(app=name, bundle_id=bundle) if name or bundle else None


def _windows() -> Optional[ForegroundWindow]:
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    user32 = windll.user32
    handle = user32.GetForegroundWindow()
    if not handle:
        return None
    length = int(user32.GetWindowTextLengthW(handle))
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, buffer, length + 1)
    return ForegroundWindow(title=buffer.value) if buffer.value else None


def _linux() -> Optional[ForegroundWindow]:
    title = _run(["xdotool", "getactivewindow", "getwindowname"]).strip()
    try:
        app = _run(["xdotool", "getactivewindow", "getwindowclassname"]).strip()
    except (OSError, subprocess.SubprocessError):
        app = ""
    return ForegroundWindow(app=app, title=title) if title or app else None


def probe_foreground() -> Optional[ForegroundWindow]:
    """The foreground app on this machine, or None when it cannot be determined."""
    probes = {"Darwin": _macos, "Windows": _windows, "Linux": _linux}
    probe = probes.get(platform.system())
    if probe is None:
        return None
    try:
        return probe()
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None
