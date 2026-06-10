from __future__ import annotations
def validate_extensions(runtime) -> dict:
    skills = runtime.skills.validate()
    plugins = {
        "enabled": runtime.cfg.plugins.enabled,
        "trusted_only": runtime.cfg.plugins.trusted_only,
        "plugins_dirs": [str(d.expanduser()) for d in runtime.cfg.workspace.plugins_dirs],
        "loaded_count": len(runtime.plugins.plugins),
        "plugins": {name: {"path": str(plugin.path), "trusted": plugin.trusted, "enabled": plugin.enabled, "tools": plugin.tools} for name, plugin in runtime.plugins.plugins.items()},
    }
    return {"ok": True, "skills": skills, "plugins": plugins, "tools": sorted(runtime.tools.names()), "planner_uses_skills": True, "dynamic_plugins_supported": True}
