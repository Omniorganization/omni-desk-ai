#!/usr/bin/env python3
"""Generates each client's contract module from apps/shared/omni-app-api.contract.json.

Each client gets the contract version it negotiates with, and requirement tables
derived from the contract's request and response schemas: for every route, the
field paths the client relies on and their JSON types (``a[].b`` is field ``b`` of
every element of array ``a``). The clients check gateway responses against these
tables at run time, so a gateway that drops or retypes a field they rely on fails
loudly instead of half-working. Their tests check the request bodies they send.

    python scripts/generate_contract_clients.py          # write the modules
    python scripts/generate_contract_clients.py --check  # fail if any is stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

CONTRACT = Path("apps/shared/omni-app-api.contract.json")
OUTPUTS = {
    "desktop": Path("apps/desktop-tauri/src/generated/contract.ts"),
    "web_admin": Path("apps/web-admin-next/lib/generated/contract.ts"),
    "mobile": Path("apps/mobile-flutter/lib/generated/contract.dart"),
}
HEADER = "Generated from apps/shared/omni-app-api.contract.json by scripts/generate_contract_clients.py. Do not edit; run the script."
JSON_TYPES = ("object", "array", "string", "number", "integer", "boolean", "null")

Requirement = tuple[str, list[str]]


def load_contract(root: Path) -> dict[str, Any]:
    return json.loads((root / CONTRACT).read_text(encoding="utf-8"))


def resolve(schema: Any, schemas: dict[str, Any]) -> Any:
    """``schema`` with every ``{"$ref": "#/schemas/Name"}`` replaced by its definition."""
    if isinstance(schema, list):
        return [resolve(item, schemas) for item in schema]
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        name = str(schema["$ref"]).removeprefix("#/schemas/")
        if name not in schemas:
            raise KeyError(f"unknown schema reference {schema['$ref']}")
        return resolve(schemas[name], schemas)
    return {key: resolve(value, schemas) for key, value in schema.items()}


def _types(schema: dict[str, Any]) -> list[str]:
    if "anyOf" in schema:
        return sorted({kind for alternative in schema["anyOf"] for kind in _types(alternative)}, key=JSON_TYPES.index)
    kinds = schema.get("type")
    if kinds is None:
        return []
    return [kinds] if isinstance(kinds, str) else list(kinds)


def _objects(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """The object shapes a schema allows (one, or the object alternatives of anyOf)."""
    if "anyOf" in schema:
        return [shape for alternative in schema["anyOf"] for shape in _objects(alternative)]
    return [schema] if "object" in _types(schema) else []


def _items(schema: dict[str, Any]) -> list[dict[str, Any]]:
    if "anyOf" in schema:
        return [item for alternative in schema["anyOf"] for item in _items(alternative)]
    return [schema["items"]] if "array" in _types(schema) and isinstance(schema.get("items"), dict) else []


def _children(schema: dict[str, Any], path: str) -> list[Requirement]:
    found: list[Requirement] = []
    for shape in _objects(schema):
        properties = shape.get("properties", {})
        for name in shape.get("required", []):
            child = properties.get(name, {})
            child_path = f"{path}.{name}" if path else name
            if _types(child):
                found.append((child_path, _types(child)))
            found.extend(_children(child, child_path))
    for item in _items(schema):
        element = f"{path}[]"
        if _types(item):
            found.append((element, _types(item)))
        found.extend(_children(item, element))
    return found


def requirements(schema: dict[str, Any]) -> list[Requirement]:
    """The required field paths of a resolved schema, with their JSON types."""
    found: list[Requirement] = [("", _types(schema))] if _types(schema) else []
    unique: dict[str, list[str]] = {}
    for path, kinds in found + _children(schema, ""):
        unique.setdefault(path, kinds)
    return list(unique.items())


def tables(contract: dict[str, Any]) -> tuple[dict[str, list[Requirement]], dict[str, list[Requirement]]]:
    schemas = contract.get("schemas", {})
    responses: dict[str, list[Requirement]] = {}
    requests: dict[str, list[Requirement]] = {}
    for endpoint in contract["endpoints"]:
        route = f"{endpoint['method']} {endpoint['path']}"
        if "response" in endpoint:
            responses[route] = requirements(resolve(endpoint["response"], schemas))
        if "request" in endpoint:
            requests[route] = requirements(resolve(endpoint["request"], schemas))
    return responses, requests


def _ts_table(name: str, table: dict[str, list[Requirement]]) -> str:
    lines = [f"export const {name}: Readonly<Record<string, readonly ContractRequirement[]>> = {{"]
    for route, items in table.items():
        lines.append(f"  {json.dumps(route)}: [")
        lines.extend(f"    [{json.dumps(path)}, {json.dumps(kinds)}]," for path, kinds in items)
        lines.append("  ],")
    lines.append("};")
    return "\n".join(lines)


TS_RUNTIME = """
/** Thrown when the gateway's answer does not meet this client's contract. */
export class ContractMismatchError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ContractMismatchError';
  }
}

function contractMajor(version: string): number | undefined {
  const match = /^(\\d{1,4})\\.(\\d{1,4})$/.exec(version.trim());
  return match ? Number(match[1]) : undefined;
}

/** Throws when the gateway speaks another major contract version (no header: a gateway from before negotiation). */
export function assertCompatibleGateway(gatewayVersion: string | null | undefined): void {
  if (gatewayVersion == null) return;
  if (contractMajor(gatewayVersion) !== contractMajor(CONTRACT_VERSION)) {
    throw new ContractMismatchError(`gateway API contract ${gatewayVersion} is incompatible with this client's ${CONTRACT_VERSION}; update the app or the gateway`);
  }
}

const ROUTES = [...new Set([...Object.keys(RESPONSE_REQUIREMENTS), ...Object.keys(REQUEST_REQUIREMENTS)])];

function trimSlashes(value: string): string[] {
  return value.replace(/^\\/+|\\/+$/g, '').split('/');
}

/** The contract route ("PATCH /app/projects/{project_id}") that a request path matches. */
export function contractRoute(method: string, path: string): string | undefined {
  const segments = trimSlashes(path.split('?', 1)[0]);
  for (const route of ROUTES) {
    const [routeMethod, template] = route.split(' ');
    if (routeMethod !== method.toUpperCase()) continue;
    const parts = trimSlashes(template);
    const matches = parts.length === segments.length
      && parts.every((part, index) => (part.startsWith('{') && part.endsWith('}') ? segments[index] !== '' : part === segments[index]));
    if (matches) return route;
  }
  return undefined;
}

function jsonType(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  if (typeof value === 'number') return Number.isInteger(value) ? 'integer' : 'number';
  return typeof value;
}

function check(value: unknown, segments: readonly string[], types: readonly string[], at: string, problems: string[]): void {
  if (segments.length === 0) {
    const actual = jsonType(value);
    if (!types.includes(actual) && !(actual === 'integer' && types.includes('number'))) {
      problems.push(`${at || 'body'}: expected ${types.join(' or ')}, got ${actual}`);
    }
    return;
  }
  // A missing, null or mistyped parent is reported by the parent's own requirement.
  if (jsonType(value) !== 'object') return;
  const [segment, ...rest] = segments;
  const each = segment.endsWith('[]');
  const name = each ? segment.slice(0, -2) : segment;
  const path = at ? `${at}.${name}` : name;
  const child = (value as Record<string, unknown>)[name];
  if (child === undefined) {
    problems.push(`${path}: missing`);
  } else if (!each) {
    check(child, rest, types, path, problems);
  } else if (Array.isArray(child)) {
    child.forEach((item, index) => check(item, rest, types, `${path}[${index}]`, problems));
  }
}

/** What `body` lacks of the fields these requirements let a client rely on. */
export function contractProblems(requirements: readonly ContractRequirement[] | undefined, body: unknown): string[] {
  const problems: string[] = [];
  for (const [path, types] of requirements || []) check(body, path ? path.split('.') : [], types, '', problems);
  return [...new Set(problems)];
}

/** Throws ContractMismatchError when a response to `method path` lacks what the contract promises. */
export function assertContractResponse(method: string, path: string, body: unknown): void {
  const route = contractRoute(method, path);
  const problems = route ? contractProblems(RESPONSE_REQUIREMENTS[route], body) : [];
  if (problems.length) throw new ContractMismatchError(`gateway response to ${route} breaks the API contract: ${problems.slice(0, 5).join('; ')}`);
}
"""


def render_ts(contract: dict[str, Any]) -> str:
    responses, requests = tables(contract)
    return "\n".join([
        f"// {HEADER}",
        "",
        f"export const CONTRACT_VERSION = {json.dumps(contract['contract_version'])};",
        "export const CONTRACT_HEADER = 'x-omnidesk-contract';",
        "",
        "/** [field path, allowed JSON types]; `a[].b` is field b of every element of array a. */",
        "export type ContractRequirement = readonly [string, readonly string[]];",
        "",
        _ts_table("RESPONSE_REQUIREMENTS", responses),
        "",
        _ts_table("REQUEST_REQUIREMENTS", requests),
        TS_RUNTIME.rstrip("\n"),
        "",
    ])


def _dart_table(name: str, table: dict[str, list[Requirement]]) -> str:
    lines = [f"const Map<String, List<ContractRequirement>> {name} = <String, List<ContractRequirement>>{{"]
    for route, items in table.items():
        lines.append(f"  {json.dumps(route)}: <ContractRequirement>[")
        lines.extend(f"    ContractRequirement({json.dumps(path)}, <String>{json.dumps(kinds)})," for path, kinds in items)
        lines.append("  ],")
    lines.append("};")
    return "\n".join(lines).replace("${", "\\${")


DART_RUNTIME = r"""
/// Thrown when the gateway's answer does not meet this client's contract.
class ContractMismatchException implements Exception {
  const ContractMismatchException(this.message);

  final String message;

  @override
  String toString() => 'ContractMismatchException: $message';
}

int? _contractMajor(String version) {
  final match = RegExp(r'^(\d{1,4})\.(\d{1,4})$').firstMatch(version.trim());
  return match == null ? null : int.parse(match.group(1)!);
}

/// Throws when the gateway speaks another major contract version (no header:
/// a gateway from before negotiation).
void assertCompatibleGateway(String? gatewayVersion) {
  if (gatewayVersion == null) return;
  if (_contractMajor(gatewayVersion) != _contractMajor(contractVersion)) {
    throw ContractMismatchException(
      "gateway API contract $gatewayVersion is incompatible with this client's $contractVersion; update the app or the gateway",
    );
  }
}

List<String> _trimSlashes(String value) =>
    value.replaceAll(RegExp(r'^/+|/+$'), '').split('/');

/// The contract route ("PATCH /app/projects/{project_id}") a request path matches.
String? contractRoute(String method, String path) {
  final segments = _trimSlashes(path.split('?').first);
  final routes = <String>{
    ...responseRequirements.keys,
    ...requestRequirements.keys,
  };
  for (final route in routes) {
    final separator = route.indexOf(' ');
    if (route.substring(0, separator) != method.toUpperCase()) continue;
    final parts = _trimSlashes(route.substring(separator + 1));
    if (parts.length != segments.length) continue;
    var matches = true;
    for (var i = 0; i < parts.length; i++) {
      final templated = parts[i].startsWith('{') && parts[i].endsWith('}');
      if (templated ? segments[i].isEmpty : parts[i] != segments[i]) {
        matches = false;
        break;
      }
    }
    if (matches) return route;
  }
  return null;
}

String _jsonType(Object? value) {
  if (value == null) return 'null';
  if (value is List) return 'array';
  if (value is Map) return 'object';
  if (value is int) return 'integer';
  if (value is num) return 'number';
  if (value is String) return 'string';
  if (value is bool) return 'boolean';
  return 'unknown';
}

void _check(
  Object? value,
  List<String> segments,
  List<String> types,
  String at,
  Set<String> problems,
) {
  if (segments.isEmpty) {
    final actual = _jsonType(value);
    if (!types.contains(actual) &&
        !(actual == 'integer' && types.contains('number'))) {
      problems.add(
        '${at.isEmpty ? 'body' : at}: expected ${types.join(' or ')}, got $actual',
      );
    }
    return;
  }
  // A missing, null or mistyped parent is reported by the parent's own requirement.
  if (value is! Map) return;
  final segment = segments.first;
  final each = segment.endsWith('[]');
  final name = each ? segment.substring(0, segment.length - 2) : segment;
  final path = at.isEmpty ? name : '$at.$name';
  if (!value.containsKey(name)) {
    problems.add('$path: missing');
    return;
  }
  final child = value[name];
  final rest = segments.sublist(1);
  if (!each) {
    _check(child, rest, types, path, problems);
  } else if (child is List) {
    for (var i = 0; i < child.length; i++) {
      _check(child[i], rest, types, '$path[$i]', problems);
    }
  }
}

/// What [body] lacks of the fields these requirements let a client rely on.
List<String> contractProblems(
  List<ContractRequirement>? requirements,
  Object? body,
) {
  final problems = <String>{};
  for (final requirement in requirements ?? const <ContractRequirement>[]) {
    _check(
      body,
      requirement.path.isEmpty ? const <String>[] : requirement.path.split('.'),
      requirement.types,
      '',
      problems,
    );
  }
  return problems.toList();
}

/// Throws when a response to [method] [path] lacks what the contract promises.
void assertContractResponse(String method, String path, Object? body) {
  final route = contractRoute(method, path);
  if (route == null) return;
  final problems = contractProblems(responseRequirements[route], body);
  if (problems.isNotEmpty) {
    throw ContractMismatchException(
      'gateway response to $route breaks the API contract: ${problems.take(5).join('; ')}',
    );
  }
}
"""


def render_dart(contract: dict[str, Any]) -> str:
    responses, requests = tables(contract)
    return "\n".join([
        f"// {HEADER}",
        "",
        f"const String contractVersion = {json.dumps(contract['contract_version'])};",
        "const String contractHeader = 'x-omnidesk-contract';",
        "",
        "/// A field path the client relies on (`a[].b`: field b of every element of",
        "/// array a) and the JSON types it may have.",
        "class ContractRequirement {",
        "  const ContractRequirement(this.path, this.types);",
        "",
        "  final String path;",
        "  final List<String> types;",
        "}",
        "",
        _dart_table("responseRequirements", responses),
        "",
        _dart_table("requestRequirements", requests),
        DART_RUNTIME.rstrip("\n"),
        "",
    ])


def render(contract: dict[str, Any]) -> dict[str, str]:
    typescript = render_ts(contract)
    return {"desktop": typescript, "web_admin": typescript, "mobile": render_dart(contract)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--check", action="store_true", help="Fail if a generated module is missing or stale.")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    rendered = render(load_contract(root))
    stale = []
    for client, relative in OUTPUTS.items():
        target = root / relative
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current == rendered[client]:
            continue
        if args.check:
            stale.append(str(relative))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rendered[client], encoding="utf-8")
            print(f"wrote {relative}")
    if stale:
        print(f"stale generated contract modules (run scripts/generate_contract_clients.py): {stale}", file=sys.stderr)
        return 1
    if args.check:
        print(f"generated contract modules are current (contract {load_contract(root)['contract_version']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
