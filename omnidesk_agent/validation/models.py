from __future__ import annotations

import os
from omnidesk_agent.models.base import ModelRequest


def validate_models(runtime) -> dict:
    router = runtime.model_router
    required_tasks = ["planner", "tool_plan", "chat", "code", "vision", "private", "summarize", "upgrade"]
    routing = router.cfg.routing
    profiles = router.providers
    coverage = {task: bool(routing.get(task) in profiles) for task in required_tasks}
    provider_aliases = sorted({
        "openai", "openai_responses", "openai_chat", "openai_compatible", "azure_openai",
        "anthropic", "claude", "gemini", "google", "ollama", "deepseek", "qwen", "dashscope",
        "groq", "mistral", "xai", "openrouter", "together", "fireworks", "perplexity",
        "moonshot", "kimi", "zhipu", "cohere", "baidu_qianfan", "bedrock",
    })
    return {
        "ok": all(coverage.values()) and bool(profiles),
        "task_coverage": coverage,
        "router": router.status(),
        "supported_provider_aliases": provider_aliases,
        "token_budget_layer": "ModelRouter",
    }


async def live_connectivity_test(runtime, profiles: list[str] | None = None) -> dict:
    router = runtime.model_router
    selected = profiles or list(router.providers.keys())
    results = {}
    for profile in selected:
        provider = router.providers.get(profile)
        if not provider:
            results[profile] = {"ok": False, "error": "profile not loaded"}
            continue
        key_env = getattr(provider.settings, "api_key_env", None)
        if key_env and not os.getenv(key_env):
            results[profile] = {"ok": False, "skipped": True, "reason": f"missing env {key_env}"}
            continue
        try:
            resp = await router.complete(ModelRequest(
                system="Return exactly: ok",
                user="Connectivity test. Return exactly: ok",
                task="chat",
                metadata={"profile": profile},
                verified_required=True,
                task_id=f"live-test-{profile}",
            ))
            results[profile] = {"ok": bool(resp.text), "provider": resp.provider, "model": resp.model, "text_preview": resp.text[:50]}
        except Exception as exc:
            results[profile] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": any(v.get("ok") for v in results.values()), "results": results}
