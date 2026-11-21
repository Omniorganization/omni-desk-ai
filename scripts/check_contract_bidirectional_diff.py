#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Gateway paths a client may call: the tri-app API, chat, and the admin routes
# Web Admin proxies.
CLIENT_PATH_PREFIXES = ("/app", "/api/chat", "/admin")
PATH_LITERAL_RE = re.compile(r"['\"`](/(?:app|api/chat|admin)[^'\"`]*)['\"`]")
OMNI_PROXY_RE = re.compile(r"omniProxy\(['\"`]([^'\"`]+)['\"`]")
# `${base}/admin/session/identity`: a path appended to an interpolated base URL.
BASE_URL_PATH_RE = re.compile(r"\$\{[^}]*\}(/(?:app|api/chat|admin)[^'\"`]*)`")
# `${expr}` (TypeScript template, Dart) or `$name` (Dart) interpolation.
INTERPOLATION_RE = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*")
EXECUTABLE_CALL_TOKENS = (
    "this.request",
    "_request",
    "fetch(",
    "fetchImpl(",
    "omniProxy(",
    "gatewayPath",
    "signRequest(",
)

CLIENT_REQUIRED_PATHS = {
    "/app/bootstrap",
    "/app/projects",
    "/app/devices/register",
    "/app/conversations",
    "/app/conversations/{conversation_id}/messages",
    "/app/conversations/{conversation_id}/ask",
    "/app/approvals/{approval_id}/decide",
    "/app/notifications",
    "/app/runtime/desktop/heartbeat",
    "/app/runtime/desktop/claim",
    "/app/sync",
    "/app/devices/{device_id}/push-token",
}

PLACEHOLDER = "{}"


def _segments(path: str) -> list[str]:
    """A path's segments, with every interpolated or templated segment as ``{}``.

    An interpolation inside a segment (``/app/approvals${url.search}``) is a
    query string or suffix and is dropped.
    """
    segments: list[str] = []
    for segment in path.strip("/").split("/"):
        if INTERPOLATION_RE.fullmatch(segment) or (segment.startswith("{") and segment.endswith("}")):
            segments.append(PLACEHOLDER)
        else:
            segments.append(INTERPOLATION_RE.sub("", segment))
    return segments


def _normalize_path(value: str) -> str | None:
    if any(ch.isspace() for ch in value) or ";" in value:
        return None
    path = value.split("?", 1)[0].rstrip(".,)")
    if not path.startswith(CLIENT_PATH_PREFIXES):
        return None
    return "/" + "/".join(_segments(path))


def _contract(root: Path) -> dict:
    return json.loads((root / "apps" / "shared" / "omni-app-api.contract.json").read_text(encoding="utf-8"))


def _contract_paths(root: Path) -> set[tuple[str, str]]:
    return {(item["method"], item["path"]) for item in _contract(root).get("endpoints", [])}


def _backend_paths(root: Path) -> set[tuple[str, str]]:
    """The gateway's real routing table, not a text search of a few modules."""
    import tempfile

    from omnidesk_agent.config import AppConfig
    from omnidesk_agent.server import create_app

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        cfg = AppConfig()
        cfg.workspace.root = base / "workspace"
        cfg.workspace.memory_db = base / "memory.sqlite3"
        cfg.workspace.skills_dirs = [base / "skills"]
        cfg.workspace.plugins_dirs = [base / "plugins"]
        cfg.permissions.audit_log = base / "audit.log"
        cfg.learning.growth_plan_file = base / "growth.json"
        cfg.channels.gmail.credentials_file = base / "google" / "credentials.json"
        cfg.channels.gmail.token_file = base / "google" / "token.json"
        from starlette.routing import WebSocketRoute

        app = create_app(cfg)
        paths: set[tuple[str, str]] = set()
        for route in app.routes:
            path = str(getattr(route, "path", ""))
            methods = {"WS"} if isinstance(route, WebSocketRoute) else set(getattr(route, "methods", None) or ()) - {"HEAD", "OPTIONS"}
            paths.update((method, path) for method in methods)
        return paths


def _is_tri_app_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in ("/app", "/api/chat"))


def _is_executable_client_context(context: str, value: str) -> bool:
    if "omniProxy(" in context and value.startswith(CLIENT_PATH_PREFIXES):
        return True
    if "gatewayPath" in context and value.startswith("/app/"):
        return True
    return any(token in context for token in EXECUTABLE_CALL_TOKENS)


def _client_paths(root: Path) -> dict[str, set[str]]:
    """Client-called paths, normalized, mapped to the files that call them.

    Static extraction: a path literal counts when it is passed through one of
    EXECUTABLE_CALL_TOKENS (the clients' request helpers) or omniProxy.
    """
    bases = [
        root / "apps" / "web-admin-next",
        root / "apps" / "desktop-tauri" / "src",
        root / "apps" / "mobile-flutter" / "lib",
    ]
    paths: dict[str, set[str]] = {}
    for base in bases:
        for suffix in ("*.ts", "*.tsx", "*.dart"):
            for path in sorted(base.rglob(suffix)):
                if "node_modules" in path.parts or ".next" in path.parts:
                    continue
                lines = path.read_text(encoding="utf-8").splitlines()
                for index, line in enumerate(lines):
                    context = "\n".join(lines[max(0, index - 3) : index + 1])
                    literals = PATH_LITERAL_RE.findall(line) + BASE_URL_PATH_RE.findall(line)
                    found = [value for value in literals if _is_executable_client_context(context, value)]
                    found += OMNI_PROXY_RE.findall(line)
                    for value in found:
                        normalized = _normalize_path(value)
                        if normalized:
                            paths.setdefault(normalized, set()).add(str(path.relative_to(root)))
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diff the shared API contract against the gateway and the clients, both ways; any drift fails.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--strict-client", action="store_true", help="Accepted for compatibility; client drift always fails now.")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    contract = _contract_paths(root)
    backend = _backend_paths(root)
    client = _client_paths(root)
    rest_contract = {item for item in contract if item[0] != "WS"}
    contract_templates = {tuple(_segments(path)): path for _, path in rest_contract}

    issues: list[str] = []
    missing_backend = sorted(contract - backend)
    if missing_backend:
        issues.append(f"contract endpoints missing backend routes: {missing_backend}")

    client_outside_contract = sorted(
        f"{path} ({', '.join(sorted(files))})" for path, files in client.items() if tuple(_segments(path)) not in contract_templates
    )
    if client_outside_contract:
        issues.append(f"client paths missing from shared contract: {client_outside_contract}")

    used = {contract_templates[tuple(_segments(path))] for path in client if tuple(_segments(path)) in contract_templates}
    missing_client_coverage = sorted(CLIENT_REQUIRED_PATHS - used)
    if missing_client_coverage:
        issues.append(f"release-critical contract paths missing client use: {missing_client_coverage}")

    backend_outside_contract = sorted(
        (method, path)
        for method, path in backend
        if _is_tri_app_path(path)
        if (method, path) not in contract
    )
    if backend_outside_contract:
        issues.append(f"backend app/chat routes missing from contract: {backend_outside_contract}")

    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print(
        "contract bidirectional diff ok: "
        f"{len(contract)} contract endpoints, {len(backend)} gateway routes, {len(client)} client paths"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
