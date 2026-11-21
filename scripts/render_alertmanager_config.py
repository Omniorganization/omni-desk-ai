"""Render deploy/observability/alertmanager.yml for one deployment.

The routing tree lives in deploy/observability/alertmanager.base.yml; this
adds the single ``omnidesk-oncall`` receiver:

* ``--receiver telegram``: Telegram bot. The bot token is read by Alertmanager
  from ``--telegram-bot-token-file`` at runtime and never written into the
  config; ``--telegram-chat-id`` is the (non-secret) chat id.
* ``--receiver webhook``: generic webhook (used by CI and the alert drill
  capture); the URL must not carry credentials in its query string.

Fails closed: an unknown receiver, a non-integer chat id or a credential-bearing
webhook URL is an error, so a broken notification path is caught before deploy.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "deploy" / "observability" / "alertmanager.base.yml"


def _telegram_receiver(chat_id: str, token_file: str) -> dict:
    if not re.fullmatch(r"-?[1-9][0-9]{0,19}", chat_id or ""):
        raise ValueError("--telegram-chat-id must be a non-zero integer chat id")
    if not token_file.startswith("/") or re.search(r"\s", token_file):
        raise ValueError("--telegram-bot-token-file must be an absolute path inside the Alertmanager container")
    return {
        "telegram_configs": [
            {
                "bot_token_file": token_file,
                "chat_id": int(chat_id),
                "send_resolved": True,
                "parse_mode": "HTML",
                "message": (
                    '{{ if eq .Status "firing" }}🔥 FIRING{{ else }}✅ RESOLVED{{ end }} '
                    "[{{ .CommonLabels.severity }}] {{ .CommonLabels.alertname }}\n"
                    "{{ range .Alerts }}{{ .Annotations.summary }}\n{{ .Annotations.runbook_url }}\n{{ end }}"
                ),
            }
        ]
    }


def _webhook_receiver(url: str) -> dict:
    parts = urlsplit(url or "")
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("--webhook-url must be an absolute http(s) URL")
    if parts.username or parts.password or parts.query:
        raise ValueError("--webhook-url must not carry credentials or a query string")
    return {"webhook_configs": [{"url": url, "send_resolved": True, "max_alerts": 50}]}


def render(receiver: str, *, chat_id: str = "", token_file: str = "", webhook_url: str = "") -> str:
    config = yaml.safe_load(BASE.read_text(encoding="utf-8"))
    if receiver == "telegram":
        spec = _telegram_receiver(chat_id, token_file)
    elif receiver == "webhook":
        spec = _webhook_receiver(webhook_url)
    else:
        raise ValueError("--receiver must be telegram or webhook")
    config["receivers"] = [{"name": "omnidesk-oncall", **spec}]
    header = "# Rendered by scripts/render_alertmanager_config.py; edit alertmanager.base.yml instead.\n"
    return header + yaml.safe_dump(config, sort_keys=False, allow_unicode=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receiver", required=True, choices=["telegram", "webhook"])
    parser.add_argument("--telegram-chat-id", default="")
    parser.add_argument("--telegram-bot-token-file", default="/etc/alertmanager/secrets/telegram_bot_token")
    parser.add_argument("--webhook-url", default="")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        text = render(
            args.receiver,
            chat_id=args.telegram_chat_id,
            token_file=args.telegram_bot_token_file,
            webhook_url=args.webhook_url,
        )
    except ValueError as exc:
        print(f"refusing to render Alertmanager config: {exc}", file=sys.stderr)
        return 2
    args.output.write_text(text, encoding="utf-8")
    print(f"wrote {args.output} (receiver={args.receiver})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
