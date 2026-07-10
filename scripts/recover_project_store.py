#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from omnidesk_agent.appsync.projects import GatewayProjectStore, ProjectStoreCorruptionError
from omnidesk_agent.appsync.store import AppSyncStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or restore a corruption-marked local OmniDesk project store from a checksum-valid backup."
    )
    parser.add_argument("--app-sync-path", required=True, help="Path to the local AppSync JSON store.")
    parser.add_argument("--backup-index", type=int, choices=(1, 2, 3))
    parser.add_argument("--actor", default="project-store-recovery-cli")
    parser.add_argument(
        "--confirm-recovery",
        action="store_true",
        help="Required to perform recovery. Without this flag the command is read-only.",
    )
    args = parser.parse_args(argv)

    app_sync = AppSyncStore(Path(args.app_sync_path))
    store = GatewayProjectStore(app_sync)
    status = store.recovery_status()
    if not args.confirm_recovery:
        print(json.dumps(status, ensure_ascii=False, sort_keys=True, indent=2))
        return 2 if status.get("blocked") else 0
    try:
        result = store.recover_from_backup(actor=args.actor, backup_index=args.backup_index)
    except (ProjectStoreCorruptionError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error_code": "PROJECT_STORE_CORRUPT", "message": str(exc)}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
