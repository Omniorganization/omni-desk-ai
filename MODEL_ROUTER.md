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
```

For Southeast Asia deployments, `qwen_dashscope_sg` is included as a disabled template profile. Replace `YOUR-WORKSPACE-ID` with the real Alibaba Cloud Model Studio workspace ID and enable that profile if a Singapore-region endpoint is required.

## Validate

```bash
omnidesk validate-models --config examples/config.yaml
```
