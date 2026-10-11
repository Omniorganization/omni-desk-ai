"""Three synthetic questions through the authenticated local Gateway; no business data."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
import uvicorn

from omnidesk_agent.config import load_config
from omnidesk_agent.server import create_app
from omnidesk_agent.storage.sqlite import close_all_open_connections


def isolated_config(path: Path, root: Path):
    cfg = load_config(path, ensure_dirs=False)
    cfg.workspace.root = root
    cfg.workspace.memory_db = root / "memory.sqlite3"
    cfg.workspace.skills_dirs = []
    cfg.workspace.plugins_dirs = []
    cfg.plugins.enabled = False
    cfg.observability.otlp_endpoint_env = f"OMNIDESK_QA_{uuid4().hex}"
    cfg.permissions.audit_log = root / "audit.log"
    cfg.app_sync.json_path = root / "appsync.json"
    cfg.channels.gmail.credentials_file = root / "google" / "credentials.json"
    cfg.channels.gmail.token_file = root / "google" / "token.json"
    cfg.learning.growth_plan_file = root / "growth.json"
    cfg.gateway.allow_local_admin_without_token = False
    cfg.gateway.allow_legacy_gateway_secret_auth = False
    for role in ("admin", "viewer", "operator", "owner"):
        setattr(cfg.gateway, f"{role}_token_env", f"OMNIDESK_QA_{uuid4().hex}")
    assert cfg.models.profiles["codex_cli"].provider == "codex_cli"
    return cfg


def checked_answer(response: httpx.Response, expected: str, model: str):
    assert response.status_code == 200, f"Gateway returned HTTP {response.status_code}"
    body = response.json()
    answer = body["assistant_message"]
    assert body["ok"] and answer["model_provider"] == "codex_cli", "Gateway provider mismatch"
    assert answer["model_name"] == model, "Gateway model mismatch"
    assert answer["content"].strip() == expected, "Gateway assistant answer mismatch"
    return {"http_status": 200, "provider": answer["model_provider"], "model": answer["model_name"], "answer": answer["content"].strip()}


def checked_stream(response: httpx.Response, expected: str, model: str):
    assert response.status_code == 200, f"Stream Gateway returned HTTP {response.status_code}"
    events = []
    for block in response.text.split("\n\n"):
        name = next((line[7:] for line in block.splitlines() if line.startswith("event: ")), None)
        data = next((line[6:] for line in block.splitlines() if line.startswith("data: ")), None)
        if name and data:
            events.append((name, json.loads(data)))
    assert not any(name == "chat.failed" for name, _ in events), "Stream Gateway emitted chat.failed"
    assert sum(name == "chat.completed" for name, _ in events) == 1, "Stream did not complete exactly once"
    deltas = [data for name, data in events if name == "chat.delta"]
    assert deltas, "Stream returned no assistant text"
    assert all(data.get("provider") == "codex_cli" and data.get("model") == model for data in deltas), "Stream provider or model mismatch"
    answer = "".join(data["text"] for data in deltas).strip()
    assert answer == expected, f"Stream assistant answer mismatch: {answer[:200]!r}"
    return {"endpoint": "/api/chat/stream", "completed_events": 1, "provider": "codex_cli", "model": model,
            "answer": answer, "native_stream": all(data["native"] for data in deltas)}


async def verify(path: Path) -> dict:
    with TemporaryDirectory(prefix="omnidesk-codex-gateway-qa-") as directory:
        cfg = isolated_config(path, Path(directory))
        owner = secrets.token_urlsafe(32)
        operator = secrets.token_urlsafe(32)
        os.environ[cfg.gateway.owner_token_env] = owner
        os.environ[cfg.gateway.operator_token_env] = operator
        app = None
        server = None
        server_task = None
        listener = socket.socket()
        try:
            app = create_app(cfg)
            # Keep background connectors/training/updaters out of a connectivity diagnostic.
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", access_log=False, log_level="critical"))
            listener.bind(("127.0.0.1", 0))
            listener.listen(16)
            listener.setblocking(False)
            server_task = asyncio.create_task(server.serve(sockets=[listener]))
            for _ in range(200):
                if server.started:
                    break
                if server_task.done():
                    await server_task
                    raise RuntimeError("local Gateway did not start")
                await asyncio.sleep(0.01)
            assert server.started
            rt = app.state.runtime
            model = cfg.models.profiles["codex_cli"].model
            headers = {"Authorization": f"Bearer {owner}", "Idempotency-Key": str(uuid4())}
            payload = {"content": "只根据本消息回答：2+3等于几？请仅输出5。", "model_profile": "codex_cli"}
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", timeout=150) as client:
                anonymous = await client.post("/api/chat", json=payload, headers={"Idempotency-Key": str(uuid4())})
                assert anonymous.status_code in {401, 403}, f"Anonymous Gateway returned HTTP {anonymous.status_code}"
                denied = await client.post("/api/chat", json=payload, headers={"Authorization": f"Bearer {operator}", "Idempotency-Key": str(uuid4())})
                assert denied.status_code == 403, f"Operator Gateway returned HTTP {denied.status_code}"
                assert rt.model_router.cost_ledger.summary()["calls"] == 0
                first = checked_answer(await client.post("/api/chat", json=payload, headers=headers), "5", model)
                assert rt.model_router.cost_ledger.summary()["calls"] == 1
                replay = checked_answer(await client.post("/api/chat", json=payload, headers=headers), "5", model)
                assert rt.model_router.cost_ledger.summary()["calls"] == 1
                second_payload = {"content": "合成算术题：每箱6件，共4箱，取出3件，剩余多少件？请仅输出21。", "model_profile": "codex_cli"}
                second = checked_answer(await client.post("/api/chat", json=second_payload, headers={**headers, "Idempotency-Key": str(uuid4())}), "21", model)
                assert rt.model_router.cost_ledger.summary()["calls"] == 2
                streaming_payload = {"content": "这是工具列表验证，不调用任何工具。请只根据当前可用工具列表判断：没有任何工具时仅输出NO_TOOLS；存在工具时仅输出TOOLS_AVAILABLE。不要解释。", "model_profile": "codex_cli"}
                stream = await client.post("/api/chat/stream", json=streaming_payload, headers={**headers, "Idempotency-Key": str(uuid4())})
                third = checked_stream(stream, "NO_TOOLS", model)
                ledger = rt.model_router.cost_ledger.summary()
                assert ledger["calls"] == 3 and ledger["durable"]["calls"] == 3
            return {
                "checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
                "scope": "local_loopback_gateway_real_codex_subscription_not_hosted_siwc",
                "success": True, "provider": "codex_cli", "model": model,
                "questions": [first, second, third],
                "anonymous_http": anonymous.status_code, "operator_profile_denied_http": denied.status_code,
                "idempotency_replay_answer": replay["answer"], "idempotency_replay_added_calls": 0,
                "successful_model_calls": ledger["calls"], "persisted_model_calls": ledger["durable"]["calls"],
                "input_tokens": ledger["input_tokens"], "output_tokens": ledger["output_tokens"],
                "platform_api_key_used": False, "model_cache_used": False, "accepted_tool_events": 0,
                "gateway_auth": "fresh_ephemeral_bearer_credentials_not_copied_from_other_services",
                "background_services_started": False, "business_data_sent": False,
            }
        finally:
            if server:
                server.should_exit = True
            if server_task:
                await asyncio.wait_for(server_task, timeout=10)
            listener.close()
            if app:
                app.state.runtime.close()
            close_all_open_connections()
            os.environ.pop(cfg.gateway.owner_token_env, None)
            os.environ.pop(cfg.gateway.operator_token_env, None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "examples/config.codex-cli.yaml")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = asyncio.run(verify(args.config))
    except Exception as exc:
        result = {"success": False, "failure_type": type(exc).__name__,
                  "failure_check": str(exc) if isinstance(exc, AssertionError) else None,
                  "scope": "local_gateway_synthetic_codex_cli_check"}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    if not result["success"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
