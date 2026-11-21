#!/usr/bin/env python3
"""Live contract run of a messaging channel against the real provider.

The run drives the production adapter code against the provider with a
dedicated test account and writes evidence describing only what this run
observed. Nothing is mocked; credentials come from the environment (GitHub
environment secrets in CI). Identifiers of people and chats are recorded as
truncated SHA-256 digests.

- slack: auth.test, then Socket Mode (no public URL). Once connected,
  ``SlackChannel.send_text`` posts a one-time code to the test channel, and
  the test user's reply must arrive as an Events API payload that
  ``SlackChannel.parse_webhook`` accepts through its allowlists.
- feishu / lark: a tenant access token, then the long connection (no public
  URL); once it is up, ``send_text`` sends the code to the test user's open_id
  (p2p) and the reply must arrive as an ``im.message.receive_v1`` event that
  ``parse_webhook`` accepts through the open_id allowlist. The console only
  saves "receive events through a long connection" while one is online, so
  the first send-receive run is the moment to save that setting.
- whatsapp: phone-number probe, then the approved ``hello_world`` template
  to the verified test recipient (send-only; inbound needs a public HTTPS
  webhook, which is deferred).
- telegram: getMe, no competing webhook, drain, ``send_text`` with the code,
  and the allowlisted reply delivered by ``TelegramPoller``.
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
from omnidesk_agent.channels.lark_feishu import FeishuChannel, LarkChannel  # noqa: E402
from omnidesk_agent.channels.lark_long_connection import LarkLongConnection  # noqa: E402
from omnidesk_agent.channels.native_messaging import SlackChannel  # noqa: E402
from omnidesk_agent.channels.provider_errors import ensure_provider_success  # noqa: E402
from omnidesk_agent.channels.slack_socket_mode import SlackSocketModeReceiver  # noqa: E402
from omnidesk_agent.channels.telegram import TelegramChannel  # noqa: E402
from omnidesk_agent.channels.telegram_polling import TelegramPoller  # noqa: E402
from omnidesk_agent.channels.whatsapp_cloud import WhatsAppCloudChannel  # noqa: E402
from omnidesk_agent.config import FeishuConfig, LarkConfig, SlackConfig, TelegramConfig, WhatsAppCloudConfig  # noqa: E402

SCHEMA = "omnidesk-channel-live-contract/v1"


class LiveContractFailure(RuntimeError):
    """A failed contract step; ``checks`` keeps what passed before it."""

    def __init__(self, message: str, *, checks: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.checks = checks or {}


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

    code = _new_code()
    text = _prompt(code, reply_timeout_seconds, "to this chat")
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
            raise LiveContractFailure(f"no reply containing the code from the allowlisted test user within {reply_timeout_seconds}s", checks=checks)
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


def _new_code() -> str:
    return "OD" + secrets.token_hex(4).upper()


def _prompt(code: str, reply_timeout_seconds: int, where: str) -> str:
    return f"OmniDesk live contract check. Reply {where} with the code {code} within {max(1, reply_timeout_seconds // 60)} minutes."


async def run_slack(*, mode: str, reply_timeout_seconds: int, http: Optional[ChannelHttpClient] = None, connect: Any = None) -> dict[str, Any]:
    bot_token = _required_env("SLACK_BOT_TOKEN")
    channel_id = _required_env("SLACK_LIVE_CHANNEL_ID")
    sender_id = _required_env("SLACK_LIVE_SENDER_ID")
    app_token = _required_env("SLACK_APP_TOKEN") if mode == "send-receive" else ""
    channel = SlackChannel(SlackConfig(enabled=True, allowed_user_ids=[sender_id], allowed_channel_ids=[channel_id]))
    if http is not None:
        channel.http = http
    checks: dict[str, Any] = {}

    auth = await channel.http.post("https://slack.com/api/auth.test", headers={"Authorization": f"Bearer {bot_token}"}, channel="slack", idempotent=True)
    ensure_provider_success("slack", auth)
    data = auth.data if isinstance(auth.data, dict) else {}
    checks["auth"] = {"passed": True, "method": "auth.test", "team_id_sha256": digest(data.get("team_id")), "bot_user_id_sha256": digest(data.get("user_id"))}

    code = _new_code()
    text = _prompt(code, reply_timeout_seconds, "in this channel")
    sent_state: dict[str, Any] = {}

    async def send() -> None:
        sent_state["at"] = time.monotonic()
        sent_state["sent"] = await channel.send_text(channel_id, text, idempotency_key=f"live-contract-{code}")

    if mode == "send-only":
        await send()
    else:
        inbox: list[Any] = []
        kwargs = {"connect": connect} if connect is not None else {}
        receiver = SlackSocketModeReceiver(channel, app_token, inbox.append, http=channel.http, **kwargs)
        # Post only once the socket is up, so the reply cannot arrive unseen.
        received = await receiver.run_until(lambda: any(code in message.text for message in inbox), timeout_seconds=reply_timeout_seconds, on_connected=send)
        checks["socket_mode"] = {"passed": "sent" in sent_state, "envelopes_acknowledged": receiver.acknowledged}
        if not received:
            if "sent" in sent_state:
                checks["send"] = {"passed": True, "adapter": "SlackChannel.send_text", "provider_message_id": sent_state["sent"].get("message_id"), "channel_id_sha256": digest(channel_id)}
            raise LiveContractFailure(f"no reply containing the code from the allowlisted test user within {reply_timeout_seconds}s", checks=checks)
        reply = next(message for message in inbox if code in message.text)
        checks["receive"] = {
            "passed": True,
            "adapter": "SlackSocketModeReceiver -> SlackChannel.parse_webhook (user and channel allowlists)",
            "provider_message_id": reply.message_id,
            "event_id": (reply.raw or {}).get("event_id"),
            "sender_id_sha256": digest(reply.sender_id),
            "seconds_after_send": round(time.monotonic() - sent_state["at"], 1),
        }
    sent = sent_state["sent"]
    if not sent.get("message_id"):
        raise LiveContractFailure("chat.postMessage returned no ts")
    checks["send"] = {"passed": True, "adapter": "SlackChannel.send_text", "provider_message_id": sent["message_id"], "channel_id_sha256": digest(channel_id)}
    if mode == "send-only":
        checks["receive"] = {"passed": None, "skipped": "send-only run"}
    return {"channel": "slack", "mode": mode, "checks": checks}


async def _run_lark_family(name: str, *, mode: str, reply_timeout_seconds: int, http: Optional[ChannelHttpClient] = None, connect: Any = None) -> dict[str, Any]:
    prefix = name.upper()
    _required_env(f"{prefix}_APP_ID")
    _required_env(f"{prefix}_APP_SECRET")
    open_id = _required_env(f"{prefix}_LIVE_OPEN_ID")
    channel = (FeishuChannel(FeishuConfig(enabled=True, allowed_open_ids=[open_id])) if name == "feishu" else LarkChannel(LarkConfig(enabled=True, allowed_open_ids=[open_id])))
    if http is not None:
        channel.http = http
    checks: dict[str, Any] = {}
    await channel._tenant_access_token()
    checks["auth"] = {"passed": True, "method": "auth/v3/tenant_access_token/internal"}
    code = _new_code()
    text = _prompt(code, reply_timeout_seconds, "to this message")
    sent_state: dict[str, Any] = {}

    async def send() -> None:
        sent_state["at"] = time.monotonic()
        sent_state["sent"] = await channel.send_text(open_id, text, idempotency_key=f"live-contract-{code}")

    def record_send() -> None:
        sent = sent_state.get("sent") or {}
        if not sent.get("message_id"):
            raise LiveContractFailure("im/v1/messages returned no message_id", checks=checks)
        checks["send"] = {"passed": True, "adapter": f"{type(channel).__name__}.send_text", "provider_message_id": sent["message_id"], "open_id_sha256": digest(open_id)}

    if mode == "send-only":
        await send()
        record_send()
        checks["receive"] = {"passed": None, "skipped": "send-only run"}
        return {"channel": name, "mode": mode, "checks": checks}

    inbox: list[Any] = []
    kwargs = {"connect": connect} if connect is not None else {}
    connection = LarkLongConnection(channel, inbox.append, http=channel.http, **kwargs)
    received = await connection.run_until(lambda: any(code in message.text for message in inbox), timeout_seconds=reply_timeout_seconds, on_connected=send)
    checks["long_connection"] = {"passed": "sent" in sent_state, "events_acknowledged": connection.acknowledged}
    if "sent" in sent_state:
        record_send()
    if not received:
        raise LiveContractFailure(f"no reply containing the code from the allowlisted test user within {reply_timeout_seconds}s", checks=checks)
    reply = next(message for message in inbox if code in message.text)
    header = (reply.raw or {}).get("header") or {}
    checks["receive"] = {
        "passed": True,
        "adapter": "LarkLongConnection -> parse_webhook (open_id allowlist)",
        "provider_message_id": reply.message_id,
        "event_id": header.get("event_id"),
        "sender_id_sha256": digest(reply.sender_id),
        "seconds_after_send": round(time.monotonic() - sent_state["at"], 1),
    }
    return {"channel": name, "mode": mode, "checks": checks}


async def run_feishu(**kwargs: Any) -> dict[str, Any]:
    return await _run_lark_family("feishu", **kwargs)


async def run_lark(**kwargs: Any) -> dict[str, Any]:
    return await _run_lark_family("lark", **kwargs)


async def run_whatsapp(*, mode: str, reply_timeout_seconds: int, http: Optional[ChannelHttpClient] = None) -> dict[str, Any]:
    _required_env("WHATSAPP_CLOUD_TOKEN")
    phone_number_id = _required_env("WHATSAPP_PHONE_NUMBER_ID")
    recipient = _required_env("WHATSAPP_LIVE_RECIPIENT")
    if mode == "send-receive":
        raise LiveContractFailure("WhatsApp inbound needs a public HTTPS webhook, which is deferred; run send-only")
    cfg = WhatsAppCloudConfig(enabled=True, phone_number_id=phone_number_id, allowed_wa_ids=[recipient])
    channel = WhatsAppCloudChannel(cfg)
    if http is not None:
        channel.http = http
    checks: dict[str, Any] = {}
    probe = await channel.http.get(
        f"https://graph.facebook.com/{cfg.graph_version}/{phone_number_id}",
        headers={"Authorization": f"Bearer {channel.token}"},
        params={"fields": "verified_name,quality_rating"},
        channel="whatsapp_cloud",
    )
    probe_data = probe.data if isinstance(probe.data, dict) else {}
    checks["auth"] = {"passed": True, "method": "GET /{phone-number-id}", "verified_name": probe_data.get("verified_name"), "graph_version": cfg.graph_version}
    sent = await channel.send_template(recipient, "hello_world", "en_US", idempotency_key=f"live-contract-{_new_code()}")
    if not sent.get("message_id"):
        raise LiveContractFailure("messages returned no wamid")
    checks["send"] = {"passed": True, "adapter": "WhatsAppCloudChannel.send_template (hello_world)", "provider_message_id": sent["message_id"], "recipient_sha256": digest(recipient)}
    checks["receive"] = {"passed": None, "skipped": "inbound webhook deferred until a public HTTPS endpoint exists"}
    return {"channel": "whatsapp", "mode": mode, "checks": checks}


RUNNERS = {"telegram": run_telegram, "slack": run_slack, "feishu": run_feishu, "lark": run_lark, "whatsapp": run_whatsapp}


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
    except LiveContractFailure as exc:
        error = f"{type(exc).__name__}: {exc}"
        result = {"checks": exc.checks}
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
