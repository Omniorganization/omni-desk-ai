#!/usr/bin/env python3
from __future__ import annotations

import ast
import sys
from pathlib import Path


def require(path: Path, *needles: str) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [f"{path}: missing {needle}" for needle in needles if needle not in text]


def parse(path: Path) -> ast.Module:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        raise RuntimeError(f"{path}: invalid Python source: {exc}") from exc


def class_methods(path: Path, class_name: str) -> set[str]:
    tree = parse(path)
    cls = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name),
        None,
    )
    if cls is None:
        return set()
    return {
        node.name
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def nested_functions(path: Path, function_name: str) -> set[str]:
    tree = parse(path)
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ),
        None,
    )
    if function is None:
        return set()
    return {
        node.name
        for node in function.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def require_methods(
    path: Path,
    class_name: str,
    required: set[str],
) -> list[str]:
    methods = class_methods(path, class_name)
    return [
        f"{path}: {class_name}.{name} must be a class-owned method"
        for name in sorted(required - methods)
    ]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    pool = root / "omnidesk_agent/repositories/postgres_pool.py"
    factory = root / "omnidesk_agent/repositories/postgres.py"
    state = root / "omnidesk_agent/repositories/postgres_state.py"
    server = root / "omnidesk_agent/server.py"
    daemon = root / "omnidesk_agent/daemon.py"

    issues: list[str] = []
    issues += require(
        pool,
        "class SharedPostgresConnectionPool",
        "def connection(",
        "def ping(",
        '"waiters"' if '"waiters"' in pool.read_text(encoding="utf-8") else "'waiters'",
    )
    issues += require_methods(
        pool,
        "SharedPostgresConnectionPool",
        {"connection", "ping", "stats", "close"},
    )
    issues += require_methods(
        factory,
        "PostgresRepositoryFactory",
        {"_connection_pool", "readiness_check", "health_check", "pool_stats", "close"},
    )
    issues += require(
        state,
        "idx_omnidesk_runs_waiting_approval",
        "idx_omnidesk_jobs_ready",
        "FOR UPDATE SKIP LOCKED",
        'return self.state.find_by_field(self.namespace, "waiting_approval_id", approval_id)',
    )
    issues += require_methods(
        state,
        "_PostgresJsonState",
        {"claim_ready_by_status", "claim_one", "ping", "pool_stats", "close"},
    )
    issues += require_methods(
        state,
        "PostgresRuntimeStateStores",
        {"readiness_check", "health_check", "close"},
    )
    issues += require(
        server,
        "await asyncio.to_thread(_readiness_snapshot_sync, deep=deep)",
        "readiness_cache",
        "await _readiness_snapshot(deep=True)",
    )
    server_nested = nested_functions(server, "create_app")
    for name in sorted({"_readiness_snapshot_sync", "_readiness_snapshot", "ready", "admin_ready"} - server_nested):
        issues.append(f"{server}: create_app.{name} must remain nested in the app factory")
    issues += require(
        daemon,
        '"whatsapp": adapters["whatsapp_cloud"]',
        '"repository_pool":',
        'getattr(self, "repository_factory", None)',
    )
    issues += require_methods(
        daemon,
        "OmniDeskRuntime",
        {"_build_channel_adapters", "_register_builtin_tools", "start", "aclose", "close"},
    )

    if issues:
        for issue in issues:
            print(f"BLOCKER {issue}", file=sys.stderr)
        return 1
    print("industrial runtime optimization contract passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
