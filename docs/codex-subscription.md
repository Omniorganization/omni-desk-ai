# OmniDesk 的 ChatGPT / Codex 订阅接入

本适配新增 `chatgpt_plan` provider，接收**为 OmniDesk 独立注册并获用户授权的 Sign in with ChatGPT（SIWC）access token**，调用官方 `https://api.openai.com/v1/responses`。它复用 OmniDesk 的模型路由、ACL、审批、token 记录和持久费用 ledger；不会读取 Codex 的 `auth.json`，不会把 Codex CLI 登录凭据当作通用 API key。

本次范围是模型协议适配。尚未实现 OmniDesk 的 OAuth 登录按钮、账户选择、ID-token 验证、刷新控制器或注销入口。外部授权宿主必须按官方流程完成独立注册、稳定 host ID、PKCE/state/nonce、ID-token 签名验证、用户同意、凭据保护、串行刷新和撤销。更新宿主注入的环境变量后，重启 OmniDesk worker 才能取得新 token。只有已有 Codex 订阅/CLI 登录而没有此独立授权时，仍不能使用本 provider。

## 证据与当前边界

2026-10-12 的独立 smoke：官方应用内置 Codex CLI `0.162.0-alpha.17.2` 使用现有 ChatGPT 登录，以 `gpt-6.1-sol` 完成一次纯合成文本请求，零工具执行。这证明当时 CLI 订阅路径可用；**不证明 SIWC 已注册，也不证明 OmniDesk 的真实订阅请求已通过**。代码测试和 GitHub PR 同样不代表部署或生产验收。

官方开放源代码/本地宿主流程与收费或远程托管应用的准入不同；后者仍需完成官方 interest form。模型也须由同一授权账号的 `GET /v1/models` 目录确认；示例 `gpt-6.1-sol` 不是每个账号的可用性承诺。

## Provider 行为

- 从 `CHATGPT_PLAN_ACCESS_TOKEN` 读取 bearer token（可通过 `api_key_env` 指定其他环境变量名），缺失即拒绝；拒绝 Platform API key 和不满足 SIWC claims 的 token。
- JWT payload 的 issuer、resource audience、整数 `exp`/`nbf` 以及 `chatgpt.tokens.use.direct`、`resource.invoke` scopes 仅作拒绝预检。预检不验证签名、用户身份或授权真实性；access-token 签名及最终授权由 OpenAI 服务端验证，授权宿主必须先验证 ID token。
- 请求固定 `store: false`、`stream: true`，系统指令放在 `instructions`，文本上下文放在 `input` 数组；不发 SIWC 不支持的 `temperature`、`max_output_tokens`、`metadata` 等字段。配置中的 output-token 值只用于本地预算估算，不能保证供方输出硬上限。
- 本适配只接文本及 JSON mode。含图片的请求明确拒绝；这属于本实现范围，不能据此称 SIWC 官方协议不支持图片。没有新增 hosted tools 或供方函数执行入口。
- 必须读取到 `response.completed` 才算成功。凭据、授权、订阅限额、传输错误、`response.failed`、`response.incomplete`、提前断流及无效 JSON 均停止当前调用，不自动重试、修复重发、切模型或转 Platform API。
- 私密任务仍走本地 Ollama。缺少本地模型时也会失败，不外发到订阅模型。

订阅请求仍消耗账号的 ChatGPT plan 使用量，计入现有 token/usage ledger；本 provider 不使用 Platform API key 或新增 Platform API 账单。它不是无限免费。ChatGPT Settings 的应用限额、共享额度及账号**已有的 credits 使用授权**由 OpenAI 管理；本代码不会自动购买或启用 credits。USD ledger 的订阅价格估算为零不代表额度剩余或没有 credits 消费，应以 [ChatGPT Settings → Usage](https://chatgpt.com/settings/usage) 为准。

## 配置与验证

独立本地示例见 [config.chatgpt-plan.yaml](../examples/config.chatgpt-plan.yaml)。它保留持久 ledger 和 token guardrail、使用独立本地记录目录，订阅路由 `fallback: []`、`max_retries: 0`。订阅 profile 包括自动路由选择，都必须通过既有角色/profile ACL；本示例仅允许现有 admin/owner 角色使用订阅。Gateway 必须从真实认证上下文取得 role/actor，不能接受调用者自行声明的身份。订阅请求不读取或写入响应缓存，缓存结果不能代替当前凭据和订阅服务验证。

该示例是本地验证配置，不是生产配置替代品。现有生产环境应只加入新的 profile 和所需路由，保留当前 RBAC、沙箱、审批、密钥、审计、存储、外部 GA 与更新证据门禁。

使用项目已隔离安装依赖的 Python，从仓库根目录执行以下离线检查；它不读取 token、不创建运行目录、不请求模型：

```bash
python - <<'PY'
from omnidesk_agent.config import load_config

c = load_config("examples/config.chatgpt-plan.yaml", ensure_dirs=False)
assert c.models.profiles["chatgpt_plan"].provider == "chatgpt_plan"
assert c.models.budget.require_persistent_ledger
for task, route in c.models.routing.items():
    if task != "profile_acl":
        assert route["fallback"] == [] and route["max_retries"] == 0
assert c.models.routing["private"]["primary"] == "local"
assert not c.gateway.allow_local_admin_without_token
assert c.permissions.no_tty_mode == "deny"
print("config passed; no inference")
PY
python -m pytest -q tests/models/test_chatgpt_plan_provider.py tests/models/test_model_router_profile_acl.py tests/test_model_router_fallback.py
```

真实 smoke 的前提是：外部授权宿主已完成授权并安全注入有效 token，真实本地管理员发起检查，账户目录已确认模型。以下命令只适用于管理员控制的本地开发终端；其中 `owner` 是该终端验证上下文，**不能复制到 Gateway 或用来绕过用户身份认证**。它会请求一次合成文本并写入本地 ledger，不运行业务任务或工具，不打印凭据、account claims 或供方原始响应：

```bash
python - <<'PY'
import asyncio
from uuid import uuid4
from omnidesk_agent.cli import runtime_context
from omnidesk_agent.config import load_config
from omnidesk_agent.models.base import ModelRequest

cfg = load_config("examples/config.chatgpt-plan.yaml")
with runtime_context(cfg) as rt:
    response = asyncio.run(rt.model_router.complete(ModelRequest(
        system="Return exactly: ok",
        user="Synthetic connectivity check. Return exactly: ok",
        task="chat",
        task_id=f"chatgpt-plan-smoke-{uuid4()}",
        metadata={"profile": "chatgpt_plan", "role": "owner", "actor": "local-cli"},
    )))
    assert response.provider == "chatgpt_plan"
    assert response.text.strip() == "ok"
    print("OmniDesk chatgpt_plan smoke passed")
PY
```

不使用既有 `validate-models-live` 全 profile 检查来替代此独立 smoke：其默认角色与本示例显式 profile ACL 不符，并会同时检查本地模型。任何失败都保留失败结果，先修复实际原因；不把“profile 已加载”“JWT 预检通过”或局部文本 delta 记为成功。

## 风险与回滚

access token 通常一小时失效；刷新/撤销由外部授权宿主负责。没有刷新控制器时，长时间运行会因过期中止。提前断流仍可能消耗订阅额度，本地成功调用 ledger 不覆盖供方未回报的失败请求，不能替代 OpenAI 的使用量账本。公开预览的模型、字段和账号准入可变，部署前需重新核验官方目录与限制。取消订阅接入时停止 worker，恢复原配置或删除新 profile/路由，并重启 worker；撤销该注册的 renewable session/应用授权由外部宿主或本人在 ChatGPT Settings 完成。保留 ledger 和审计，不删除历史记录。

## 官方依据

- [SIWC 开放源代码 / 本地宿主概览](https://developers.openai.com/siwc/token-sharing-open-source)
- [独立注册、登录与授权](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [模型目录与流式推理](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [账户、刷新、撤销与使用量](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [Token claims 与有效期](https://developers.openai.com/siwc/token-sharing-open-source/token-reference)
- [预览限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
