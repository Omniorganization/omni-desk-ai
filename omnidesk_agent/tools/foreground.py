"""Which application is in the foreground, for the UI bridge's visible-target rule.

The UI bridge may act only in the app the operator named, while that app is the
visible foreground window and shows a signed-in session. These probes ask the
operating system, read-only, and return None when they cannot tell; callers
must then refuse to act (fail closed).

- macOS: `lsappinfo` (front application name and bundle id; no window title).
- Windows: the foreground window title (user32).
- Linux/X11: `xdotool` (active window title and class), when installed.

Matching is strict. When the OS names the application (the macOS name and
bundle id, the X11 window class), that identity decides: the bundle id must be
one known for the app, or the name must be the app's name or one of its
localized names. The window title counts only when the OS names no application
(Windows) or names a web browser, which may show the app's web version. A title
matches when one of its separator-delimited parts is the app's name, so
"WhatsApp Business" is not WhatsApp and "系统信息" is not Messages.
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

# Other names the same app shows under: localized names, and the long form of a short name.
APP_ALIASES: dict[str, tuple[str, ...]] = {
    "google chrome": ("chrome",),
    "chrome": ("google chrome",),
    "edge": ("microsoft edge",),
    "wechat": ("微信", "weixin"),
    "feishu": ("飞书",),
    "dingtalk": ("钉钉",),
    "messages": ("信息",),
    "xiaohongshu": ("小红书", "rednote"),
}

# macOS bundle ids, compared whole and ignoring case. Each was read from the app's
# Info.plist on macOS 27.2.1; an app missing here is matched by its name only.
BUNDLE_IDS: dict[str, tuple[str, ...]] = {
    "wechat": ("com.tencent.xinWeChat",),
    "whatsapp": ("net.whatsapp.WhatsApp",),
    "telegram": ("ru.keepcoder.Telegram",),
    "dingtalk": ("5ZSL2CJU2T.com.dingtalk.mac",),
    "messages": ("com.apple.MobileSMS",),
    "google chrome": ("com.google.Chrome",),
    "chrome": ("com.google.Chrome",),
    "safari": ("com.apple.Safari",),
}

# Applications that show other apps' web versions (normalized names and X11 classes).
_BROWSERS = frozenset({"googlechrome", "chrome", "chromium", "microsoftedge", "firefox", "safari", "bravebrowser"})

# Window titles of sign-in, QR-login, verification and password screens.
_SIGN_IN_RE = re.compile(
    r"\b(log ?in|sign ?in|signin|sign up|verify (your|it's)|verification|two[- ]factor|2fa|passcode|password)\b"
    r"|登录|登入|扫码|扫一扫|验证|认证|密码|ログイン|로그인",
    re.IGNORECASE,
)
# Title parts: "Chat | Microsoft Teams", "Home / X - Google Chrome", "Telegram (12)".
_TITLE_SEPARATOR_RE = re.compile(r"\s+[-–—/|·]\s+|\s*\|\s*")
_UNREAD_COUNT_RE = re.compile(r"^\(\d+\+?\)\s*|\s*\(\d+\+?\)$")
# `lsappinfo info -only name -only bundleid ASN` prints a header line with the
# (localized) name, then `bundleID="..."`; `-long` prints quoted key/value pairs.
_LSAPPINFO_NAME_RE = re.compile(r'^"(.*)" ASN:', re.MULTILINE)
_LSAPPINFO_BUNDLE_RE = re.compile(r'^\s*bundleID="([^"]*)"', re.MULTILINE)
_KEY_VALUE_RE = re.compile(r'"([^"]+)"\s*=\s*"([^"]*)"')


@dataclass(frozen=True)
class ForegroundWindow:
    app: str = ""  # application name (macOS) or window class (X11), as the OS reports it
    title: str = ""  # window title, when the OS reports it
    bundle_id: str = ""  # macOS bundle identifier

    def describe(self) -> str:
        return " / ".join(part for part in (self.app, self.title, self.bundle_id) if part) or "unknown window"


def _norm(value: str) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").casefold())


def _names(app: str) -> set[str]:
    key = app.strip().casefold()
    return {name for name in (_norm(app), *(_norm(alias) for alias in APP_ALIASES.get(key, ()))) if name}


def _title_parts(title: str) -> set[str]:
    return {_norm(_UNREAD_COUNT_RE.sub("", part.strip())) for part in _TITLE_SEPARATOR_RE.split(title)} - {""}


def window_matches_app(window: ForegroundWindow, app: str) -> bool:
    """Whether the foreground window belongs to `app`; see the module docstring for the rules."""
    names = _names(app)
    if not names:
        return False
    bundles = {bundle.casefold() for bundle in BUNDLE_IDS.get(app.strip().casefold(), ())}
    if window.bundle_id and window.bundle_id.casefold() in bundles:
        return True
    identity = _norm(window.app)
    if identity in names:
        return True
    if window.bundle_id or (identity and identity not in _BROWSERS):
        return False  # the OS names another application; its window title does not count
    return bool(window.title) and not names.isdisjoint(_title_parts(window.title))


def looks_like_sign_in(window: ForegroundWindow) -> bool:
    return bool(window.title and _SIGN_IN_RE.search(window.title))


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace", timeout=_TIMEOUT_SECONDS, check=True).stdout  # nosec B603


def parse_lsappinfo_info(output: str) -> Optional[ForegroundWindow]:
    name = _LSAPPINFO_NAME_RE.search(output)
    bundle = _LSAPPINFO_BUNDLE_RE.search(output)
    pairs = dict(_KEY_VALUE_RE.findall(output))
    app = name.group(1) if name else pairs.get("LSDisplayName", "")
    bundle_id = bundle.group(1) if bundle else pairs.get("CFBundleIdentifier", "")
    return ForegroundWindow(app=app, bundle_id=bundle_id) if app or bundle_id else None


def _macos() -> Optional[ForegroundWindow]:
    front = _run(["lsappinfo", "front"]).strip()
    if not front:
        return None
    return parse_lsappinfo_info(_run(["lsappinfo", "info", "-only", "name", "-only", "bundleid", front]))


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
