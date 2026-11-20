#!/usr/bin/env python3
"""Live contract run of a messaging channel against the real provider.

The run drives the production adapter code (``TelegramChannel.send_text``,
``TelegramPoller`` and ``TelegramChannel.parse_update``) against the provider
with a dedicated test account, and writes evidence describing only what this
run observed. Nothing is mocked; credentials come from the environment (GitHub
environment secrets in CI). Identifiers of people and chats are recorded as
truncated SHA-256 digests.

Telegram send-receive:
1. ``getMe`` authenticates the bot token.
2. ``getWebhookInfo`` must show no webhook: polling cannot run beside one,
   and the run never deletes a webhook.
3. Pending updates are drained so only replies to this run count.
4. ``send_text`` posts a message carrying a one-time code to the test chat.
5. The test user replies with the code; the poller must deliver that reply
   through the allowlist within the timeout.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omnidesk_agent.channels.http_client import ChannelHttpClient  # noqa: E402
from omnidesk_agent.channels.provider_errors import ensure_provider_success  # noqa: E402
from omnidesk_agent.channels.telegram import TelegramChannel  # noqa: E402
from omnidesk_agent.channels.telegram_polling import TelegramPoller  # noqa: E402
from omnidesk_agent.config import TelegramConfig  # noqa: E402

SCHEMA = "omnidesk-channel-live-contract/v1"


class LiveContractFailure(RuntimeError):
    pass


def digest(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise LiveContractFailure(f"{name} is not set (configure it as a secret of the channel-live environment)")
    return value


def run_metadata() -> dict[str, Any]:
    server = os.getenv("GITHUB_SERVER_URL", "")
    repository = os.getenv("GITHUB_REPOSITORY", "")
    run_id = os.getenv("GITHUB_RUN_ID", "")
    return {
        "run_url": f"{server}/{repository}/actions/runs/{run_id}" if server and repository and run_id else None,
        "run_id": run_id or None,
        "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT") or None,
        "workflow": os.getenv("GITHUB_WORKFLOW") or None,
        "commit": os.getenv("GITHUB_SHA") or None,
        "ref": os.getenv("GITHUB_REF") or None,
    }


async def run_telegram(*, mode: str, reply_timeout_seconds: int, http: Optional[ChannelHttpClient] = None, poll_http: Optional[ChannelHttpClient] = None) -> dict[str, Any]:
    _required_env("TELEGRAM_BOT_TOKEN")
    chat_id = _required_env("TELEGRAM_LIVE_CHAT_ID")
    sender_raw = _required_env("TELEGRAM_LIVE_SENDER_ID")
    try:
        sender_id = int(sender_raw)
    except ValueError as exc:
        raise LiveContractFailure("TELEGRAM_LIVE_SENDER_ID must be the numeric Telegram user id of the test account") from exc
    channel = TelegramChannel(TelegramConfig(enabled=True, allowed_user_ids=[sender_id]))
    if http is not None:
        channel.http = http
    client = channel.http
    checks: dict[str, Any] = {}

    me = await client.get(f"{channel.base}/getMe", channel="telegram")
    ensure_provider_success("telegram", me)
    bot = me.data.get("result") if isinstance(me.data, dict) else None
    if not isinstance(bot, dict) or bot.get("is_bot") is not True:
        raise LiveContractFailure("getMe did not return a bot")
    checks["auth"] = {"passed": True, "method": "getMe", "bot_username": bot.get("username"), "bot_id_sha256": digest(bot.get("id"))}

    webhook = await client.get(f"{channel.base}/getWebhookInfo", channel="telegram")
    ensure_provider_success("telegram", webhook)
    webhook_url = (webhook.data.get("result") or {}).get("url") if isinstance(webhook.data, dict) else None
    if webhook_url:
        raise LiveContractFailure("the test bot has a webhook set; polling cannot run beside it (use a dedicated test bot without a webhook)")
    checks["no_webhook_for_polling"] = {"passed": True}

    inbox: list[Any] = []
    poller = TelegramPoller(channel, inbox.append, timeout_seconds=25, http=poll_http)
    drained = 0
    while True:
        confirmed = await poller.poll_once(timeout_seconds=0)
        drained += confirmed
        if not confirmed:
            break
    inbox.clear()
    checks["drain"] = {"passed": True, "pending_updates_confirmed": drained}

    code = "OD" + secrets.token_hex(4).upper()
    text = f"OmniDesk live contract check. Reply to this chat with the code {code} within {max(1, reply_timeout_seconds // 60)} minutes."
    sent_at = time.monotonic()
    sent = await channel.send_text(chat_id, text, idempotency_key=f"live-contract-{code}")
    if not sent.get("message_id"):
        raise LiveContractFailure("sendMessage returned no message_id")
    checks["send"] = {
        "passed": True,
        "adapter": "TelegramChannel.send_text",
        "provider_message_id": sent["message_id"],
        "chat_id_sha256": digest(chat_id),
    }

    if mode == "send-only":
        checks["receive"] = {"passed": None, "skipped": "send-only run"}
        return {"channel": "telegram", "mode": mode, "checks": checks}

    deadline = sent_at + reply_timeout_seconds
    reply = None
    while reply is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LiveContractFailure(f"no reply containing the code from the allowlisted test user within {reply_timeout_seconds}s")
        await poller.poll_once(timeout_seconds=int(min(25, max(1, remaining))))
        reply = next((message for message in inbox if code in message.text), None)
    checks["receive"] = {
        "passed": True,
        "adapter": "TelegramPoller.poll_once -> TelegramChannel.parse_update (allowlist)",
        "provider_message_id": reply.message_id,
        "update_id": (reply.raw or {}).get("update_id"),
        "sender_id_sha256": digest(reply.sender_id),
        "seconds_after_send": round(time.monotonic() - sent_at, 1),
    }
    return {"channel": "telegram", "mode": mode, "checks": checks}


RUNNERS = {"telegram": run_telegram}


def build_evidence(result: Optional[dict[str, Any]], *, channel: str, mode: str, error: Optional[str]) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "schema": SCHEMA,
        "channel": channel,
        "mode": mode,
        "status": "passed" if error is None else "failed",
        "produced_at": datetime.now(timezone.utc).isoformat(),
        **run_metadata(),
        "checks": (result or {}).get("checks", {}),
    }
    if error is not None:
        evidence["error"] = error
    return evidence


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--channel", required=True, choices=sorted(RUNNERS))
    parser.add_argument("--mode", default="send-receive", choices=["send-receive", "send-only"])
    parser.add_argument("--reply-timeout-seconds", type=int, default=600)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    error = None
    result = None
    try:
        result = asyncio.run(RUNNERS[args.channel](mode=args.mode, reply_timeout_seconds=max(30, args.reply_timeout_seconds)))
    except Exception as exc:  # the evidence records the failure; the exit code fails the job
        error = f"{type(exc).__name__}: {exc}"
    evidence = build_evidence(result, channel=args.channel, mode=args.mode, error=error)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"channel": args.channel, "status": evidence["status"], "output": str(output), "error": error}, ensure_ascii=False))
    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
