from __future__ import annotations

def validate_models(runtime) -> dict:
    required=['planner','tool_plan','chat','code','vision','private','summarize','upgrade']
    coverage={task: bool(runtime.model_router.cfg.routing.get(task) in runtime.model_router.providers) for task in required}
    aliases=sorted(['openai','openai_responses','openai_chat','openai_compatible','azure_openai','anthropic','claude','gemini','google','ollama','deepseek','qwen','dashscope','groq','mistral','xai','openrouter','together','fireworks','perplexity','moonshot','kimi','zhipu','cohere','baidu_qianfan','bedrock'])
    return {'ok': all(coverage.values()) and bool(runtime.model_router.providers), 'task_coverage': coverage, 'router': runtime.model_router.status(), 'supported_provider_aliases': aliases, 'token_budget_layer':'ModelRouter'}
