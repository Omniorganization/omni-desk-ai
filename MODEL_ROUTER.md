# Omni-deskAi Model Router

`TokenBudgetManager` has been moved to `ModelRouter`.

Flow:

```text
ModelRequest -> ModelRouter route by task -> TokenBudgetManager decide/cache/trim/override -> Provider adapter -> usage audit
```

## Supported provider aliases

`openai`, `openai_responses`, `openai_chat`, `openai_compatible`, `azure_openai`, `anthropic`, `claude`, `gemini`, `google`, `ollama`, `deepseek`, `qwen`, `dashscope`, `groq`, `mistral`, `xai`, `openrouter`, `together`, `fireworks`, `perplexity`, `moonshot`, `kimi`, `zhipu`, `cohere`, `baidu_qianfan`, `bedrock`.

## DeepSeek and Qwen routing

`examples/config.yaml` enables both OpenAI-compatible production profiles:

- `deepseek`: `provider=deepseek`, `model=deepseek-v4-pro`, `api_key_env=DEEPSEEK_API_KEY`, `base_url=https://api.deepseek.com`
- `qwen_dashscope`: `provider=dashscope`, `model=qwen-plus`, `api_key_env=DASHSCOPE_API_KEY`, `base_url=https://dashscope.aliyuncs.com/compatible-mode/v1`

Default routing now prefers DeepSeek for normal chat and summarization, and Qwen/DashScope for code and upgrade tasks. Each route keeps OpenAI and/or local Ollama fallbacks so the runtime can degrade safely if the external provider is unavailable.

To activate live calls, provide the secrets before running the gateway or validation:

```bash
export DEEPSEEK_API_KEY="..."
export DASHSCOPE_API_KEY="..."
omnidesk validate-models --config examples/config.yaml
omnidesk validate-models-live --config examples/config.yaml
```

The example config also allows `operator` live validation of the explicit `deepseek`, `qwen_dashscope`, and disabled-template `qwen_dashscope_sg` profiles through `models.routing.profile_acl`. The existing `code` OpenAI profile remains the only high-cost profile in the sample ACL, so the live smoke can reach these new providers when their credentials are present without bypassing the high-cost approval gate.

For Southeast Asia deployments, `qwen_dashscope_sg` is included as a disabled template profile. Replace `YOUR-WORKSPACE-ID` with the real Alibaba Cloud Model Studio workspace ID and enable that profile if a Singapore-region endpoint is required.

## Cost and budget guardrails

The router estimates projected spend before provider execution and records fallback cost in the ledger when an OpenAI-compatible provider omits exact billed cost. `ModelPricingTable` includes non-zero fallback entries for:

- `deepseek/deepseek-v4-pro`
- `deepseek/*`
- `dashscope/qwen-plus`
- `dashscope/*`
- `qwen/*`

These values are server-side guardrail estimates, not client-controlled request metadata. Operators should update the table to match their contracted billing tier before promoting a live deployment.

## Governance evidence

Owner approval: this change is limited to example profile routing and server-side budget/ACL guardrails. Merging requires the normal protected-branch review and required checks.

Risk notes:

- Expands outbound model-provider routes for deployments that copy `examples/config.yaml` and provide `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY`.
- Secrets remain environment-driven and are not committed.
- Routing retains OpenAI/local fallbacks for provider outage handling.
- Budget enforcement now has non-zero fallback pricing for the new external provider aliases.

Rollback steps:

1. Set `models.profiles.deepseek.enabled=false` and `models.profiles.qwen_dashscope.enabled=false`, or restore `models.routing.chat`, `models.routing.summarize`, `models.routing.code`, and `models.routing.upgrade` to OpenAI/local profiles.
2. Remove `DEEPSEEK_API_KEY` and `DASHSCOPE_API_KEY` from the runtime environment.
3. Run `omnidesk validate-models --config examples/config.yaml` and restart the gateway.

Validation evidence to collect before production activation:

```bash
omnidesk validate-models --config examples/config.yaml
omnidesk validate-models-live --config examples/config.yaml
omnidesk production-check --config examples/config.yaml
```

The live validation command should be run only in a controlled environment with provider credentials and expected network egress approvals.

## Validate

```bash
omnidesk validate-models --config examples/config.yaml
```
