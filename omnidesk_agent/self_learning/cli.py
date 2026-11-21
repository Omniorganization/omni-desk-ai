"""Operator CLI for learned artefacts.

    python -m omnidesk_agent.self_learning.cli [--config PATH] <command>

Commands: list, show, request-approval, decide, apply, rollback, active, history.
`decide`, `apply` and `rollback` need an explicit --reviewer/--actor; the OS
account that ran the command is recorded next to it.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from omnidesk_agent.self_learning.approval import HumanApprovalGate, is_system_identity
from omnidesk_agent.self_learning.observability.audit import LearningAuditLog
from omnidesk_agent.self_learning.policy_registry import PolicyRegistry
from omnidesk_agent.self_learning.store import SelfLearningStore


def registry_paths(workspace_root: Path) -> dict[str, Path]:
    root = Path(workspace_root).expanduser()
    return {
        "store": root / "self_learning.sqlite3",
        "registry": root / "learning_policy.sqlite3",
        "audit": root / "learning_audit.jsonl",
    }


@contextmanager
def open_registry(workspace_root: Path) -> Iterator[PolicyRegistry]:
    paths = registry_paths(workspace_root)
    store = SelfLearningStore(paths["store"])
    try:
        yield PolicyRegistry(paths["registry"], approvals=HumanApprovalGate(store), audit=LearningAuditLog(paths["audit"]))
    finally:
        store.close()


def _os_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def _identity(name: str) -> str:
    person = str(name or "").strip()
    if is_system_identity(person):
        raise PermissionError("a human identity is required (not a system or agent name)")
    return f"{person} (os-user:{_os_user()})"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m omnidesk_agent.self_learning.cli")
    parser.add_argument("--config", default="~/.omnidesk/config.yaml")
    parser.add_argument("--workspace", default=None, help="workspace root (overrides the config file)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    list_p = sub.add_parser("list")
    list_p.add_argument("--status", action="append", default=None)
    list_p.add_argument("--limit", type=int, default=50)
    show_p = sub.add_parser("show")
    show_p.add_argument("artifact_id")
    req_p = sub.add_parser("request-approval")
    req_p.add_argument("artifact_id")
    dec_p = sub.add_parser("decide")
    dec_p.add_argument("approval_id")
    dec_p.add_argument("decision", choices=["approved", "rejected"])
    dec_p.add_argument("--reviewer", required=True)
    dec_p.add_argument("--reason", default="")
    apply_p = sub.add_parser("apply")
    apply_p.add_argument("artifact_id")
    apply_p.add_argument("--actor", required=True)
    apply_p.add_argument("--reason", default="")
    rb_p = sub.add_parser("rollback")
    rb_p.add_argument("--actor", required=True)
    rb_p.add_argument("--reason", required=True)
    rb_p.add_argument("--to-version", default=None)
    sub.add_parser("active")
    hist_p = sub.add_parser("history")
    hist_p.add_argument("--limit", type=int, default=20)
    return parser


def run(argv: Optional[list[str]] = None, *, workspace_root: Optional[Path] = None) -> dict[str, Any]:
    args = build_parser().parse_args(argv)
    root = workspace_root or (Path(args.workspace) if args.workspace else _workspace_from_config(args.config))
    with open_registry(root) as registry:
        if args.cmd == "list":
            return {"artifacts": [a.to_dict() for a in registry.list_artifacts(statuses=args.status, limit=args.limit)]}
        if args.cmd == "show":
            artifact = registry.get(args.artifact_id)
            if artifact is None:
                raise KeyError(args.artifact_id)
            return artifact.to_dict()
        if args.cmd == "request-approval":
            return registry.request_approval(args.artifact_id).to_dict()
        if args.cmd == "decide":
            return registry.decide(args.approval_id, args.decision, reviewer=_identity(args.reviewer), reason=args.reason).to_dict()
        if args.cmd == "apply":
            return registry.apply(args.artifact_id, actor=_identity(args.actor), reason=args.reason).to_dict()
        if args.cmd == "rollback":
            return registry.rollback(actor=_identity(args.actor), reason=args.reason, to_version=args.to_version).to_dict()
        if args.cmd == "active":
            return registry.active_policy().to_dict()
        if args.cmd == "history":
            return {"history": registry.history(limit=args.limit)}
    raise ValueError(f"unknown command {args.cmd}")  # pragma: no cover


def _workspace_from_config(path: str) -> Path:
    from omnidesk_agent.config import load_config

    return load_config(path).workspace.root


def main(argv: Optional[list[str]] = None) -> int:
    try:
        print(json.dumps(run(argv), ensure_ascii=False, indent=2))
        return 0
    except (KeyError, PermissionError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
