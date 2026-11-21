#!/usr/bin/env python3
"""Start or renew Gmail push notifications (users.watch) for the configured account.

Gmail stops pushing to the Pub/Sub topic seven days after the last watch
call, so run this at least daily (cron, systemd timer, Kubernetes CronJob).
The first call also stores the starting history cursor next to the token.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omnidesk_agent.channels.gmail import GmailChannel  # noqa: E402
from omnidesk_agent.config import load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, help="OmniDesk config.yaml")
    parser.add_argument("--topic", required=True, help="projects/<project>/topics/<topic> that gmail-api-push@system.gserviceaccount.com may publish to")
    args = parser.parse_args(argv)
    cfg = load_config(Path(args.config))
    if not cfg.channels.gmail.enabled:
        print("channels.gmail.enabled is false", file=sys.stderr)
        return 2
    response = GmailChannel(cfg.channels.gmail).start_watch(args.topic)
    print(json.dumps({"historyId": response.get("historyId"), "expiration": response.get("expiration")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
