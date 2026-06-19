#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from omnidesk_agent.security.audit_worm import WormAuditCheckpoint  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or verify append-only audit checkpoints.")
    parser.add_argument("--audit-log", required=True)
    parser.add_argument("--checkpoint-dir", required=True)
    parser.add_argument("--verify-checkpoint")
    args = parser.parse_args(argv)
    writer = WormAuditCheckpoint(Path(args.checkpoint_dir))
    if args.verify_checkpoint:
        ok = writer.verify(Path(args.verify_checkpoint), Path(args.audit_log))
        print("audit checkpoint verify ok" if ok else "audit checkpoint verify failed")
        return 0 if ok else 1
    checkpoint = writer.create(Path(args.audit_log))
    print(f"audit checkpoint created: {checkpoint.checkpoint_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
