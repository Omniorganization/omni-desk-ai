from __future__ import annotations
from typing import Optional

import os
from omnidesk_agent.models.base import ModelRequest
from omnidesk_agent.models.providers import PROVIDER_CLASSES


REQUIRED_TASKS = ["planner", "tool_plan", "chat", "code", "vision", "private", "summarize", "upgrade"]


def env_var_status(variable: Optional[str]) -> str:
    """'present' or 'missing' for the environment variable a profile names; 'not_required' when it names none.

    Only whether the variable is set is reported, never its value; a profile without
    one (a local Ollama, a subscription CLI) is usable but unverified until a live check.
    """
    if not variable:
        return "not_required"
    return "present" if os.getenv(variable) else "missing"


def route_profiles(routing: dict, task: str, default: str = "") -> list[str]:
    raw = routing.get(task) or routing.get("chat") or default
    if isinstance(raw, dict):
        primary = str(raw.get("primary") or default)
        return [primary] + [str(item) for item in raw.get("fallback", []) if str(item) != primary]
    return [str(raw)] if raw else []


def validate_models(runtime) -> dict:
    router = runtime.model_router
    profiles = router.providers
    # Presence of each profile's key variable, not the key: no value is read into the report.
    env_presence = {name: env_var_status(getattr(getattr(provider, "settings", None), "api_key_env", None)) for name, provider in profiles.items()}
    coverage = {}
    tasks = {}
    for task in REQUIRED_TASKS:
        if hasattr(router, "route_plan"):
            plan = router.route_plan(task)
            route = list(getattr(plan, "profiles", []))
        else:
            route = route_profiles(router.cfg.routing, task)
        usable = [profile for profile in route if profile in profiles and env_presence[profile] != "missing"]
        tasks[task] = {
            "profiles": route,
            "usable": usable,
            "without_key_check": [profile for profile in usable if env_presence[profile] == "not_required"],
            "missing_credentials": [profile for profile in route if env_presence.get(profile) == "missing"],
        }
        # Every routed profile must be loaded, and one of them must have its key (or need none).
        coverage[task] = bool(route) and all(profile in profiles for profile in route) and bool(usable)
    provider_aliases = sorted(PROVIDER_CLASSES)
    return {
        "ok": all(coverage.values()) and bool(profiles),
        "task_coverage": coverage,
        "tasks": tasks,
        "credentials": env_presence,
        "note": "static check of key variables only; `doctor` names the variables to set, validate-models-live calls each profile",
        "router": router.status(),
        "supported_provider_aliases": provider_aliases,
        "token_budget_layer": "ModelRouter",
    }


async def live_connectivity_test(runtime, profiles: Optional[list[str]] = None) -> dict:
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
