# Runtime Closure Upgrades

This update connects the previously added infrastructure into the main execution chain.

## Completed

1. `ToolRegistry.describe()` is now implemented.
2. Core tools have explicit `spec()` definitions:
   - computer
   - browser
   - gmail
   - files
   - shell
   - ui_bridge
   - vision
3. `PermissionManager` now supports:
   - `interactive_cli`
   - `remote_approval`
   - `auto_policy`
4. `RunStore` persists resumable runs.
5. `/agent/resume/{run_id}` calls `orchestrator.resume(run_id)`.
6. `VisionActionExecutor` can convert grounding targets into computer clicks when `auto_click_grounded=true`.
7. `ShellTool` now uses allowlisted argv execution with `create_subprocess_exec`, not `create_subprocess_shell`.
8. New tests cover:
   - ToolRegistry.describe
   - remote approval
   - shell allowlist
   - RunStore lifecycle

## Remaining

- Run resume currently replays from stored step index, but approval correlation can be stricter.
- Vision verify/retry policy should be expanded.
- Tool specs should be made more detailed action-by-action.
- Plugin signature and subprocess isolation remain next-stage work.
