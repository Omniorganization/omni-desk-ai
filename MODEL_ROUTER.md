# Omni-deskAi Model Router

`TokenBudgetManager` has been moved to `ModelRouter`.

Flow:

```text
ModelRequest -> ModelRouter route by task -> TokenBudgetManager decide/cache/trim/override -> Provider adapter -> usage audit
```

## Supported provider aliases

`openai`, `openai_responses`, `openai_chat`, `openai_compatible`, `azure_openai`, `anthropic`, `claude`, `gemini`, `google`, `ollama`, `deepseek`, `qwen`, `dashscope`, `groq`, `mistral`, `xai`, `openrouter`, `together`, `fireworks`, `perplexity`, `moonshot`, `kimi`, `zhipu`, `cohere`, `baidu_qianfan`, `bedrock`.

## Validate

```bash
omnidesk validate-models --config examples/config.yaml
```
