# OmniDesk 的 ChatGPT / Codex 订阅接入

本适配提供两条独立路径：

| Provider | 使用条件 | 范围 |
| --- | --- | --- |
| `codex_cli` | 本人本机已安装官方 Codex CLI，并已使用 ChatGPT 登录 | 本地 OmniDesk 调用官方 CLI；登录和刷新由 CLI 管理 |
| `chatgpt_plan` | 为 OmniDesk 独立注册并获用户授权的 SIWC access token | 官方 Responses 协议适配；OAuth 生命周期由外部授权宿主管理 |

两条路径都保留 OmniDesk 的角色/profile ACL、审批、预算、token 记录和持久费用 ledger。订阅 profile 包括自动路由选择，都必须通过既有 ACL；示例仅允许真实认证上下文中的 admin/owner 使用订阅。Gateway 从认证结果取得 role/actor，调用者不能自行声明身份。订阅失败不重试、不修复重发、不转付费 profile，也不读取或写入响应缓存。私密任务仍走本地 Ollama；本地模型不可用时停止。

## 本人本机的 Codex CLI 路径

独立配置见 [config.codex-cli.yaml](../examples/config.codex-cli.yaml)。它使用已有的官方 CLI ChatGPT 登录，无需为此路径重新注册 SIWC 应用。OmniDesk 不提取、复制或打印桌面登录凭据；不要填写 API key、共享账户凭据或将登录目录打包到配置中。此配置供本人本机使用，不能据此提供远程托管或其他人的订阅服务。

当前实现只接受已核对的精确 CLI 版本 `0.162.0-alpha.17.2`，本机来源为官方应用内置 CLI，模型为 `gpt-6.1-sol`。其它版本在完成兼容性验证前拒绝调用，不把更高版本视为自动兼容。默认优先使用本机官方应用内置 CLI，再查找 PATH；可在 profile 中设置可信的绝对 `codex_cli_path`。CLI、模型元数据或配置字段变化后应重新验证，未知字段或不兼容模型应停止。

每次调用启动官方 CLI 的 **AppServer stdio JSON-RPC** transport，使用内部临时 provider 别名 `omnidesk_codex_subscription`，复用 CLI 管理的 OpenAI/ChatGPT 认证和官方服务地址，并将 request/stream retries 设为零。OmniDesk 对外仍是 `codex_cli` provider；不接受自定义 HTTP 地址或 API key，不修改全局配置。

provider 当前仅支持具有 POSIX 进程组清理能力的本机宿主；本轮实际宿主为 macOS。Windows 在初始化时拒绝接入，Linux 尚未完成真实订阅验收。每个 CLI 子进程拥有独立进程组，超时、取消或错误后清理整个进程组，并限制清理等待；这不构成其它平台的已验证支持。

从仓库根目录，使用项目已隔离安装依赖的 Python。以下检查不请求模型、不读取登录凭据：

```bash
python - <<'PY'
from omnidesk_agent.config import load_config

c = load_config("examples/config.codex-cli.yaml", ensure_dirs=False)
assert c.models.profiles["codex_cli"].provider == "codex_cli"
assert c.models.profiles["codex_cli"].api_key_env is None
assert c.models.budget.require_persistent_ledger
assert c.models.routing["chat"]["fallback"] == []
assert c.models.routing["chat"]["max_retries"] == 0
assert c.models.routing["private"]["primary"] == "local"
assert not c.gateway.allow_local_admin_without_token
assert c.permissions.no_tty_mode == "deny"
print("config passed; no inference")
PY
python -m pytest -q tests/models/test_codex_cli_provider.py
```

provider 测试使用假 CLI，并屏蔽真实 CLI 自动发现。它们只能证明本地契约，不能证明账号有订阅额度或模型权限。

本人在本机安全设置好 Gateway 认证后，可启动本地服务。CLI 模块的入口是 `main()`，启动命令实际调用该入口；端口应选择本机空闲端口：

```bash
python -c 'from omnidesk_agent.cli import main; main()' --config examples/config.codex-cli.yaml serve --host 127.0.0.1 --port 19005
```

此 `serve` 命令启动配置中的正常服务。独立连通性验收使用下方脚本：它另建临时 loopback Gateway、临时本地认证和存储，关闭后台服务，只提交三个合成问题；不需要把其他服务的认证凭据复制给脚本。**该命令会真实调用已登录账号并消耗订阅额度**：

```bash
python -m scripts.verify_codex_cli_chat --config examples/config.codex-cli.yaml
```

验收需同时检查匿名访问被拒绝、operator 不能使用订阅 profile、真实 owner 请求的模型与答案匹配、幂等重放不新增调用、SSE 完成事件及持久用量记录。脚本结果的 `success` 必须真实通过所有断言；脚本存在、profile 已加载或独立 CLI 回答成功不能代替这次 Gateway 验收。

AppServer 请求使用临时工作目录、严格配置检查、只读沙箱和临时会话；`thread/start` 必须回报预期 model、provider、cwd、`readOnly`、`on-request` 审批及允许的指令来源，门禁通过后才能 `turn/start`。临时模型目录将 `shell_type` 设为 `disabled`、`apply_patch_tool_type` 设为 `null`、`experimental_supported_tools` 清空、`tool_mode` 收紧为该版本支持的 `direct`，同时关闭 `code_mode` / `code_mode_only`、搜索及其它已知工具能力。只读沙箱本身仍可能允许读文件，不能单凭 `read-only` 宣称工具全禁。

AppServer 不支持 `--ignore-user-config`，所以先 `initialize`、再内部 `config/read`，先验证有效订阅别名的固定协议、认证、零重试及官方服务地址，拒绝额外非空认证/传输字段和端点覆盖；再仅投影全局 MCP server 名称并在 `thread/start` 逐个设为 `enabled: false`，不输出配置值。空 `{}` 会深度合并，不能靠它清空全局 MCP。当前进程还清空配置项中的全局 developer instructions，关闭 history 持久化、analytics 和 OTel exporter/trace exporter、禁止日志记录用户 prompt，并设置 `RUST_LOG=off`。这些限制不等于完全不读取宿主配置。

官方协议没有关闭全局 AGENTS 文件的入口，`project_doc_max_bytes=0` 只约束项目文档。来源门禁仅接受本人 `CODEX_HOME` 顶层 `AGENTS.md` 或 `AGENTS.override.md`：目录和文件必须属于当前本人 UID，且不可由 group/world 写入，文件须为普通文件；符号链接解析后离开该目录即拒绝。来源字段缺失、非法或其它来源均停止请求。适配器只校验来源路径和文件元数据，不读取或打印内容；官方 CLI 自身仍可加载这些本人常规指令，不修改全局配置或复制认证。

只忽略已知无动作的 remote-control、skills、account、app 状态通知。异常警告、工具项、服务端 action/approval 请求、模型改道、未允许的指令来源或未知事件均拒绝，停止当前请求；不自动批准。未接受工具事件仅是当前 CLI/model/config 组合的有限证据，不证明绝对零工具执行或未来所有能力都已关闭。

本次进程关闭 `agents.enabled`，将模型的 `multi_agent_version` 设为 `disabled`；同时关闭技能目录自动提示、内置技能和 cloud skills。`skip_host_skill_discovery` 仅在没有注册贡献者要求宿主技能发现时跳过 host snapshots；它不是无条件禁止宿主资料读取的开关。普通文本中的技能提及仍可能由已注册的技能贡献者解析，本轮合成题没有请求技能。CLI 自身需要读取已保存的认证，因此本验收不宣称宿主资料绝对零读取或所有技能输入均已禁止。`suppress_unstable_features_warning` 只抑制该实验功能的启动信息提示，保留严格配置、沙箱、审批和事件拒绝门禁。

临时 `models.json` 只保存官方公开的 base/canonical 指令及收紧工具能力的静态元数据，不含请求 system、历史上下文或用户正文。公开 canonical 指令通过 `thread/start.baseInstructions` 指定；可信的 `ModelRequest.system` 通过独立 `thread/start.developerInstructions` 传递；用户正文在 `turn/start.input` 中单独传递。请求内容经 stdin JSON-RPC 进入子进程，不写入该目录文件、命令参数或环境变量。成功响应须完成 thread/turn 生命周期、完整助手消息及唯一成功完成事件，input/output token 用量必须是非负整数。未完成或用量不合法的请求停止，不记为成功。

当前 `codex_cli` 的 `/api/chat/stream` 是**缓冲 SSE**：先等待 CLI 完整成功回答，再发出文本和完成事件，`native: false`。它不能提供逐 token 的原生实时推送。含图片的请求拒绝，JSON mode 输出不合法时停止。配置中的 `max_output_tokens` 用于本地预算估算，不是 CLI 输出的硬上限。

## 证据与当前验收状态

2026-10-12，官方应用内置 CLI `0.162.0-alpha.17.2` 使用现有 ChatGPT 登录，以 `gpt-6.1-sol` 完成独立纯合成文本 smoke，JSONL 未接受工具事件。这只证明当时该本机 CLI/账号/model 的请求可用。

**旧 exec transport 的完整本地合成验收已通过**，时间为 `2026-10-12T06:05:46.853759+08:00`。真实 loopback Gateway 经认证、chat API、router 和官方 CLI 完成三次合成问答：普通问答严格回答 `5`、`21`，SSE 工具列表问题严格回答 `NO_TOOLS`。匿名和 operator 访问均返回 HTTP 403；幂等重放新增调用为零；成功调用和持久 ledger 均为 3 次，共 15,260 input tokens、31 output tokens；SSE 为 `native: false`。未使用 Platform API key、响应缓存或后台服务，未发送业务数据。历史证据保留于 [此前本地 Gateway 验收摘要](evidence/codex-cli-gateway-20261012.json)，它不代表当前 AppServer transport 已完成真实推理验收。

`NO_TOOLS` 是模型对当前工具列表的自述，JSONL 策略证明该次未接受工具事件；两者不能独立证明绝对零工具执行、宿主零读取或未来版本也安全。旧版本离线相关测试 173 项通过（`-Werror`，其中 provider 测试 70 项），Ruff 通过。当前 AppServer provider 离线测试 180 项通过，7 个相关文件共 283 项通过（`-W error`）；Ruff、import graph 通过，Pyright 0 errors / 1,005 warnings，未做警告基线差分。假 CLI 测试不替代真实请求。

已推送提交 `744ba691` 的全部 GitHub CI 已完成：53 项成功、2 项跳过、1 项失败。唯一失败为 CodeQL，报告编号 #15 的一条 high finding，涉及可信 system/context 追加到目录后写入明文 `models.json`。当前 AppServer 改动将请求上下文移出目录文件，尚需新提交的 CI 重新验证，不能宣称全绿。

匿名目录管道的正负控曾证明 CLI 能读取管道，但真实执行需要重复读取目录，EOF 后不能再次读取，因此当前改用 AppServer。此前 `thread/start` 的全局 `AGENTS.md` 来源被“来源必须为空”的假设拒绝；当前已改为上述本人文件来源门禁，真实诊断已完成 turn。**当前 AppServer 完整本地三题验收通过**，时间为 `2026-10-12T06:47:01.467044+08:00`：HTTP 严格回答 `5`、`21`，缓冲 SSE 严格回答 `NO_TOOLS`；匿名/operator 均 403，幂等重放新增调用 0，成功及持久 ledger 均 3，共 15,450 input / 135 output tokens。未使用 Platform API key、响应缓存或后台服务，未发送业务数据。[当前 AppServer 验收摘要](evidence/codex-cli-gateway-security-20261012.json)绑定源码哈希；新提交的 GitHub CI 仍须在推送后独立核对。源码、单测、PR 或历史本地问答通过都不代表托管部署或生产验收；独立 SIWC 授权及其真实推理仍未验证。

## 独立 SIWC 路径

`chatgpt_plan` provider 接收**为 OmniDesk 独立注册并获用户授权的 Sign in with ChatGPT（SIWC）access token**，调用官方 `https://api.openai.com/v1/responses`。它不会读取 Codex 的 `auth.json`，不会把 Codex CLI 登录凭据当作通用 API key。

本次范围是模型协议适配。尚未实现 OmniDesk 的 OAuth 登录按钮、账户选择、ID-token 验证、刷新控制器或注销入口。外部授权宿主必须按官方流程完成独立注册、稳定 host ID、PKCE/state/nonce、ID-token 签名验证、用户同意、凭据保护、串行刷新和撤销。更新宿主注入的环境变量后，重启 OmniDesk worker 才能取得新 token。只有已有 Codex 订阅/CLI 登录而没有此独立授权时，仍不能使用本 provider。

官方开放源代码/本地宿主流程与收费或远程托管应用的准入不同；后者仍需完成官方 interest form。模型也须由同一授权账号的 `GET /v1/models` 目录确认；示例 `gpt-6.1-sol` 不是每个账号的可用性承诺。

### SIWC provider 行为

- 从 `CHATGPT_PLAN_ACCESS_TOKEN` 读取 bearer token（可通过 `api_key_env` 指定其他环境变量名），缺失即拒绝；拒绝 Platform API key 和不满足 SIWC claims 的 token。
- JWT payload 的 issuer、resource audience、整数 `exp`/`nbf` 以及 `chatgpt.tokens.use.direct`、`resource.invoke` scopes 仅作拒绝预检。预检不验证签名、用户身份或授权真实性；access-token 签名及最终授权由 OpenAI 服务端验证，授权宿主必须先验证 ID token。
- 请求固定 `store: false`、`stream: true`，系统指令放在 `instructions`，文本上下文放在 `input` 数组；不发 SIWC 不支持的 `temperature`、`max_output_tokens`、`metadata` 等字段。配置中的 output-token 值只用于本地预算估算，不能保证供方输出硬上限。
- 本适配只接文本及 JSON mode。含图片的请求明确拒绝；这属于本实现范围，不能据此称 SIWC 官方协议不支持图片。没有新增 hosted tools 或供方函数执行入口。
- 必须读取到 `response.completed` 才算成功。凭据、授权、订阅限额、传输错误、`response.failed`、`response.incomplete`、提前断流及无效 JSON 均停止当前调用，不自动重试、修复重发、切模型或转 Platform API。
- 私密任务仍走本地 Ollama。缺少本地模型时也会失败，不外发到订阅模型。

订阅请求仍消耗账号的 ChatGPT plan 使用量，计入现有 token/usage ledger；本 provider 不使用 Platform API key 或新增 Platform API 账单。它不是无限免费。ChatGPT Settings 的应用限额、共享额度及账号**已有的 credits 使用授权**由 OpenAI 管理；本代码不会自动购买或启用 credits。USD ledger 的订阅价格估算为零不代表额度剩余或没有 credits 消费，应以 [ChatGPT Settings → Usage](https://chatgpt.com/settings/usage) 为准。

### SIWC 配置与验证

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

CLI 登录到期、撤销、订阅额度不足或 CLI/model 不兼容时，当前请求停止，由本人通过官方 CLI 处理登录状态。SIWC access token 通常一小时失效；刷新/撤销由外部授权宿主负责。没有刷新控制器时，长时间运行会因过期中止。

提前断流或失败仍可能消耗订阅额度，本地成功调用 ledger 不覆盖供方未回报的失败请求，不能替代 OpenAI 的使用量账本。两条订阅路径都不新增 Platform API key 账单，也不自动购买或启用 credits；账号已有的 credits 使用授权仍由 OpenAI 管理。零 USD 估算不表示无限免费或额度剩余，应核对 [ChatGPT Settings → Usage](https://chatgpt.com/settings/usage)。

模型、字段和账号准入可变，使用前需重新核验对应路径。取消接入时停止 worker，恢复原配置或删除新 profile/路由，并重启 worker；独立 SIWC 应用授权撤销由外部宿主或本人在 ChatGPT Settings 完成。保留 ledger 和审计，不删除历史记录。

## 官方依据

- [Codex 认证与 ChatGPT 登录](https://learn.chatgpt.com/docs/auth)
- [Codex AppServer 集成协议](https://learn.chatgpt.com/docs/app-server)
- [Codex CLI 非交互执行与 JSON 事件](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Codex CLI 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)
- [SIWC 开放源代码 / 本地宿主概览](https://developers.openai.com/siwc/token-sharing-open-source)
- [独立注册、登录与授权](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [模型目录与流式推理](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [账户、刷新、撤销与使用量](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [Token claims 与有效期](https://developers.openai.com/siwc/token-sharing-open-source/token-reference)
- [预览限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
