from __future__ import annotations

import sys
from pathlib import Path

import yaml


def _github_actions_triggers(data: dict) -> object:
    # PyYAML YAML 1.1 parses the GitHub Actions key `on` as boolean True.
    return data.get("on", data.get(True))


def _check_dispatch_input_limit(path: Path, triggers: object) -> bool:
    if not isinstance(triggers, dict):
        return True
    dispatch = triggers.get("workflow_dispatch")
    if not isinstance(dispatch, dict):
        return True
    inputs = dispatch.get("inputs", {})
    if not isinstance(inputs, dict):
        return True
    if len(inputs) <= 10:
        return True
    print(
        f"{path}: workflow_dispatch defines {len(inputs)} inputs; GitHub Actions supports at most 10",
        file=sys.stderr,
    )
    return False


def _check_workflow_call_required_defaults(path: Path, triggers: object) -> bool:
    if not isinstance(triggers, dict):
        return True
    workflow_call = triggers.get("workflow_call")
    if not isinstance(workflow_call, dict):
        return True
    inputs = workflow_call.get("inputs", {})
    if not isinstance(inputs, dict):
        return True

    ok = True
    for name, definition in inputs.items():
        if not isinstance(definition, dict):
            continue
        if definition.get("required") is True and "default" in definition:
            ok = False
            print(
                f"{path}: workflow_call input {name!r} cannot be both required and have a default",
                file=sys.stderr,
            )
    return ok


def main(argv: list[str] | None = None) -> int:
    root = Path(argv[0]) if argv else Path(".github/workflows")
    if not root.exists():
        print(f"workflow directory not found: {root}", file=sys.stderr)
        return 2
    ok = True
    for path in sorted(root.glob("*.yml")) + sorted(root.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - failure path is CLI-focused
            ok = False
            print(f"{path}: invalid YAML: {exc}", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            ok = False
            print(f"{path}: workflow root must be a mapping", file=sys.stderr)
            continue
        triggers = _github_actions_triggers(data)
        if triggers is None:
            ok = False
            print(f"{path}: missing GitHub Actions 'on' trigger", file=sys.stderr)
        if "jobs" not in data:
            ok = False
            print(f"{path}: missing jobs section", file=sys.stderr)
        if not _check_dispatch_input_limit(path, triggers):
            ok = False
        if not _check_workflow_call_required_defaults(path, triggers):
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
