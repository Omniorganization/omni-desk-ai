#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from omnidesk_agent.core.outbound_messages import OutboundMessageStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover stale outbound sends and report ambiguous sends requiring reconciliation/manual review.")
    parser.add_argument("db", type=Path, help="Path to outbound_messages.sqlite3")
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--ambiguous-limit", type=int, default=50)
    parser.add_argument("--requeue-ambiguous", action="store_true", help="Operator override: requeue ambiguous sends after external provider verification.")
    args = parser.parse_args()
    store = OutboundMessageStore(args.db)
    recovered = store.recover_stale_running(lease_seconds=args.lease_seconds)
    ambiguous = store.list_ambiguous(limit=args.ambiguous_limit)
    requeued: list[str] = []
    if args.requeue_ambiguous:
        for item in ambiguous:
            store.requeue(str(item["id"]))
            requeued.append(str(item["id"]))
        ambiguous = store.list_ambiguous(limit=args.ambiguous_limit)
    print(json.dumps({
        "ok": True,
        "recovered_stale_running": recovered,
        "ambiguous_count": len(ambiguous),
        "ambiguous": [
            {
                "id": item["id"],
                "channel": item["channel"],
                "recipient": item["recipient"],
                "idempotency_key": item["idempotency_key"],
                "provider_request_id": item.get("provider_request_id"),
                "error_category": item.get("error_category"),
                "last_error": item.get("last_error"),
            }
            for item in ambiguous
        ],
        "requeued_ambiguous": requeued,
        "stats": store.stats(),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
