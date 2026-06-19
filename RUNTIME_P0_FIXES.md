# Runtime P0 Fixes

This update closes the most important runtime correctness gaps.

## Fixed

1. Slots dataclass serialization:
   - replaced `msg.__dict__` / `plan.__dict__` with `dataclasses.asdict`
   - added `core/serialization.py`
   - added `plan_from_dict` and `message_from_dict`

2. Resume step index:
   - approval suspension now saves the actual `idx`
   - no longer uses `len(results)` as a step index

3. PlanStep naming:
   - unified to `requires_approval`
   - `requires_confirmation` remains as a compatibility alias

4. Gmail OAuth state:
   - server route no longer sends static `"omnidesk-gmail"` state
   - OAuth manager always creates one-time stored state

5. Resume token:
   - RunStore now generates `resume_token`
   - `/agent/resume/{run_id}` must include `resume_token`

6. Approval scope binding:
   - proposals include `run_id`, `plan_id`, `step_index`, and `scope_hash`
   - resume verifies the exact approval scope

7. Vision coordinate scaling:
   - screenshot returns original/scaled dimensions and scale ratio
   - grounded click coordinates are mapped back to real screen coordinates

8. Runtime wiring:
   - model router is built before registering VisionTool
   - PluginRegistry now supports list-based plugin dirs and PluginConfig input
   - webhook guard is available on runtime

9. Safer shell default:
   - `git push`, `git commit`, and `pip install -e` are only enabled when `shell_upgrade_enabled=true`
