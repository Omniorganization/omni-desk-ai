from __future__ import annotations
def validate_extensions(runtime) -> dict:
    skills = runtime.skills.validate()
    loaded_plugins = getattr(runtime.plugins, "plugins", getattr(runtime.plugins, "loaded", {}))
    plugins = {
        "enabled": runtime.cfg.plugins.enabled,
        "trusted_only": runtime.cfg.plugins.trusted_only,
        "allowlist": sorted(runtime.cfg.plugins.allowlist),
        "plugins_dirs": [str(d.expanduser()) for d in runtime.cfg.workspace.plugins_dirs],
        "loaded_count": len(loaded_plugins),
        "plugins": {
            name: {
                "version": plugin.version,
                "trusted": plugin.trusted,
                "enabled": plugin.enabled,
                "sandbox": plugin.sandbox,
                "entrypoint": plugin.entrypoint,
                "permissions": list(plugin.permissions),
            }
            for name, plugin in sorted(loaded_plugins.items())
        },
    }
    return {"ok": True, "skills": skills, "plugins": plugins, "tools": sorted(runtime.tools.names()), "planner_uses_skills": True, "dynamic_plugins_supported": True}
