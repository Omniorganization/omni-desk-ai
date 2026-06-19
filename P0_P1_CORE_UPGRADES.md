# P0/P1 Core Upgrades

Implemented:

## P0

1. Fixed duplicate `[project.optional-dependencies]` in `pyproject.toml`.
2. Added `websockets` to base dependencies and kept optional groups clean.
3. Hardened `BrowserTool.evaluate`:
   - disabled by default through `ChromeConfig.allow_evaluate = false`
   - blocks deny patterns such as `document.cookie`, `localStorage`, `indexedDB`, `fetch(`
   - checks current tab origin before CDP actions.
4. Gmail OAuth now uses least-privilege scopes and one-time OAuth state validation:
   - `readonly`
   - `allow_compose`
   - `allow_send`
   - `allow_modify`
   - `oauth_redirect_allowlist`
   - `OAuthStateStore`.

## P1

1. Added `PlanSchema` and `PlanValidator`.
2. Added `ToolSpec` and `ToolRegistry.describe()`.
3. Added `LLMStructuredPlanner`.
4. Added remote approval exception path:
   - `ApprovalRequired`
   - `approval_mode = remote_approval`
   - Orchestrator returns `waiting_approval`.
5. Connected screenshot results to `vision.ground` when an image path is produced.
6. Orchestrator now sanitizes large / unsafe result fields before returning.
