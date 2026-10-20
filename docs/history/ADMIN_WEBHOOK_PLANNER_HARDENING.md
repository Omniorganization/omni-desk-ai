# Admin / Webhook / Planner Hardening

Implemented changes:

1. Management APIs use unified `AdminAuth`.
2. WebhookSecurity is called for Telegram, WhatsApp, Meta, WeChat, DingTalk, Lark, Feishu, LINE and X POST webhooks.
3. `PermissionError` from webhook security is translated to HTTP 403.
4. Channel adapters expose `extract_envelope()` so webhook guards use real `sender_id` and `message_id`.
5. `PermissionManager.session_allows` is scoped by `source + actor + risk + scope_hash`.
6. `RunStore` generates one-time resume tokens only while waiting for approval and clears them after completion.
7. `ToolSelector` reduces planner context before calling the LLM.
8. Low-confidence vision targets now request approval instead of failing.
9. `PlanValidator` reuses `ActionSpec.validate_args`.
10. Server uses `rt.approval_store`; it no longer creates a separate approval store.
11. Core Pyright gate is provided via `pyrightconfig.json` and `scripts/check_core_pyright.sh`.

Run:

```bash
python3 -m compileall omnidesk_agent
python3 -m pytest tests/test_admin_auth.py tests/test_permission_session_scope.py tests/test_run_store_resume_token_lifecycle.py tests/test_tool_selector_and_plan_validator.py tests/test_webhook_envelopes.py tests/test_vision_low_confidence_approval.py -q
scripts/check_core_pyright.sh
```
