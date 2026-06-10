# Security Hardening and CI Upgrade

Implemented:

1. RunStore approval binding:
   - stores `waiting_approval_id`
   - stores `approval_proposal_json`
   - resume checks approval is approved and proposal fields match.

2. Vision verify/retry:
   - `VisionActionExecutor.verify_with_retry`
   - step-level `verification`
   - step-level `retry_policy`
   - screenshot -> vision verify loop.

3. Strict ToolSpec JSON Schema:
   - `obj_schema`
   - `ActionSpec.validate_args`
   - PlanValidator validates required args, basic types and unknown args.

4. Webhook security middleware:
   - replay protection
   - idempotency
   - per-source rate limit
   - timestamp window
   - LINE/HMAC helpers.

5. Plugin hardening:
   - `PluginManifest`
   - sha256 verification
   - HMAC signature verification
   - permission declaration
   - subprocess plugin runner.

6. CI:
   - GitHub Actions workflow
   - compileall
   - ruff
   - pytest
   - pyright basic check.
