"""Render deploy/observability/alertmanager.yml for one deployment.

The routing tree lives in deploy/observability/alertmanager.base.yml; this
adds the single ``omnidesk-oncall`` receiver:

* ``--receiver slack``: a Slack incoming webhook (ideally posting to a
  dedicated alerts channel). The webhook URL is a secret: Alertmanager reads it
  from ``--slack-webhook-url-file`` at runtime and it is never written into the
  config. Operators store it as the ``OMNIDESK_ALERT_SLACK_WEBHOOK_URL``
  environment secret and place it in that file at deploy time.
* ``--receiver webhook``: a generic webhook (CI and alert-drill capture); the
  URL must not carry credentials in its query string.

Fails closed: an unknown receiver, a relative secret path, a malformed channel
name or a credential-bearing webhook URL is an error, so a broken notification
path is caught before deploy.
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
SLACK_TITLE = '{{ if eq .Status "firing" }}FIRING{{ else }}RESOLVED{{ end }} [{{ .CommonLabels.severity }}] {{ .CommonLabels.alertname }}'
SLACK_TEXT = "{{ range .Alerts }}{{ .Annotations.summary }}\n{{ .Annotations.runbook_url }}\n{{ end }}"


def _slack_receiver(url_file: str, channel: str) -> dict:
    if not url_file.startswith("/") or re.search(r"\s", url_file):
        raise ValueError("--slack-webhook-url-file must be an absolute path inside the Alertmanager container")
    if channel and not re.fullmatch(r"#[a-z0-9][a-z0-9_-]{0,79}", channel):
        raise ValueError("--slack-channel must look like #omnidesk-alerts")
    config = {"api_url_file": url_file, "send_resolved": True, "title": SLACK_TITLE, "text": SLACK_TEXT}
    if channel:
        config["channel"] = channel
    return {"slack_configs": [config]}


def _webhook_receiver(url: str) -> dict:
    parts = urlsplit(url or "")
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("--webhook-url must be an absolute http(s) URL")
    if parts.username or parts.password or parts.query:
        raise ValueError("--webhook-url must not carry credentials or a query string")
    return {"webhook_configs": [{"url": url, "send_resolved": True, "max_alerts": 50}]}


def render(receiver: str, *, slack_url_file: str = "", slack_channel: str = "", webhook_url: str = "") -> str:
    config = yaml.safe_load(BASE.read_text(encoding="utf-8"))
    if receiver == "slack":
        spec = _slack_receiver(slack_url_file, slack_channel)
    elif receiver == "webhook":
        spec = _webhook_receiver(webhook_url)
    else:
        raise ValueError("--receiver must be slack or webhook")
    config["receivers"] = [{"name": "omnidesk-oncall", **spec}]
    header = "# Rendered by scripts/render_alertmanager_config.py; edit alertmanager.base.yml instead.\n"
    return header + yaml.safe_dump(config, sort_keys=False, allow_unicode=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receiver", required=True, choices=["slack", "webhook"])
    parser.add_argument("--slack-webhook-url-file", default="/etc/alertmanager/secrets/slack_webhook_url")
    parser.add_argument("--slack-channel", default="")
    parser.add_argument("--webhook-url", default="")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        text = render(
            args.receiver,
            slack_url_file=args.slack_webhook_url_file,
            slack_channel=args.slack_channel,
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
