#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

BOUNDARY_PREFIXES = {
    "self_learning": "omnidesk_agent.self_learning",
    "appsync": "omnidesk_agent.appsync",
    "channels": "omnidesk_agent.channels",
    "integrations": "omnidesk_agent.integrations",
    "server_routes": "omnidesk_agent.server_routes",
    "tools": "omnidesk_agent.tools",
    "self_upgrade": "omnidesk_agent.self_upgrade",
}

FORBIDDEN_IMPORTS = {
    "omnidesk_agent.self_learning": {
        "omnidesk_agent.api",
        "omnidesk_agent.appsync",
        "omnidesk_agent.channels",
        "omnidesk_agent.integrations",
        "omnidesk_agent.server",
        "omnidesk_agent.server_routes",
        "omnidesk_agent.self_upgrade",
        "omnidesk_agent.tools",
    },
    "omnidesk_agent.channels": {
        "omnidesk_agent.appsync",
        "omnidesk_agent.self_learning",
        "omnidesk_agent.self_upgrade",
        "omnidesk_agent.server_routes",
    },
    "omnidesk_agent.integrations": {
        "omnidesk_agent.appsync",
        "omnidesk_agent.self_learning",
        "omnidesk_agent.self_upgrade",
    },
}


def _module_name(root: Path, path: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_context(path: Path, module: str) -> str:
    """Return the package context used by relative imports in this file."""
    if path.name == "__init__.py":
        return module
    return module.rsplit(".", 1)[0]


def _resolve_relative(package: str, level: int, imported: str | None) -> str:
    parts = package.split(".") if package else []
    if level > 1:
        parts = parts[: max(0, len(parts) - (level - 1))]
    if imported:
        parts.extend(imported.split("."))
    return ".".join(parts)


def _import_from_targets(node: ast.ImportFrom, path: Path, module: str) -> set[str]:
    if node.level:
        base = _resolve_relative(_package_context(path, module), node.level, node.module)
    else:
        base = node.module or ""
    if not base:
        return set()
    targets = {base}
    for alias in node.names:
        if alias.name == "*":
            continue
        targets.add(f"{base}.{alias.name}")
    return targets


def _imports_for(path: Path, module: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            imports.update(_import_from_targets(node, path, module))
    return {item for item in imports if item.startswith("omnidesk_agent")}


def _boundary_for(module: str) -> str:
    for prefix in BOUNDARY_PREFIXES.values():
        if module == prefix or module.startswith(prefix + "."):
            return prefix
    return "omnidesk_agent"


def _tarjan(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[list[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal index
        indexes[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in graph.get(node, set()):
            if target not in indexes:
                strongconnect(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[target])
        if lowlinks[node] == indexes[node]:
            component: list[str] = []
            while True:
                item = stack.pop()
                on_stack.remove(item)
                component.append(item)
                if item == node:
                    break
            if len(component) > 1:
                components.append(sorted(component))

    for node in sorted(graph):
        if node not in indexes:
            strongconnect(node)
    return components


def build_report(root: Path) -> dict:
    source_root = root / "omnidesk_agent"
    modules: dict[str, set[str]] = {}
    for path in sorted(source_root.rglob("*.py")):
        module = _module_name(root, path)
        modules[module] = _imports_for(path, module)

    boundary_edges: dict[str, set[str]] = defaultdict(set)
    forbidden: list[dict[str, str]] = []
    for module, imports in modules.items():
        source_boundary = _boundary_for(module)
        for imported in imports:
            if imported not in modules and not any(
                known.startswith(imported + ".") for known in modules
            ):
                continue
            target_boundary = _boundary_for(imported)
            if source_boundary != target_boundary:
                boundary_edges[source_boundary].add(target_boundary)
            for forbidden_source, targets in FORBIDDEN_IMPORTS.items():
                if module == forbidden_source or module.startswith(forbidden_source + "."):
                    if any(imported == item or imported.startswith(item + ".") for item in targets):
                        forbidden.append({"module": module, "import": imported})

    boundary_graph = {
        key: sorted(value) for key, value in sorted(boundary_edges.items())
    }
    cycles = _tarjan({key: set(value) for key, value in boundary_graph.items()})
    return {
        "module_count": len(modules),
        "edge_count": sum(len(value) for value in modules.values()),
        "boundary_edges": boundary_graph,
        "boundary_cycles": cycles,
        "forbidden_imports": forbidden,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check OmniDesk import graph boundaries.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--write-report", default="")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    report = build_report(root)
    if args.write_report:
        output = Path(args.write_report)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    if report["forbidden_imports"]:
        print("forbidden import graph edges detected:", file=sys.stderr)
        for item in report["forbidden_imports"]:
            print(f"  {item['module']} -> {item['import']}", file=sys.stderr)
        return 1
    print(
        "import graph ok: "
        f"{report['module_count']} modules, {report['edge_count']} internal edges"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
