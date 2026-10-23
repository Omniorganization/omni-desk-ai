# Claude Code CLI 订阅接入（待真实验收）

`claude_code_cli` 通过本人安装并登录的官方 CLI 使用订阅额度，与 `codex_cli`、`chatgpt_plan` 共用角色/profile ACL、预算、持久 ledger、禁响应缓存及失败停止规则。示例 [config.claude-code-cli.yaml](../examples/config.claude-code-cli.yaml) 默认禁用新 profile；不要在真实兼容性验收前启用。

## 实现与检查

- 固定兼容目标为 `2.1.302`；来自原补丁的目标版本，本次没有真实 CLI 验证，其它版本拒绝。
- 仅允许 `auth status --json` 返回 `loggedIn=true`、`authMethod=claude.ai`、`apiProvider=firstParty`。未知字段值、API key、OAuth 环境 token、第三方云认证均拒绝；实际个人订阅的字段组合仍须确认。
- 子进程只继承有限环境；不接受 API key、base URL、额外 headers/body。认证仍由 CLI 自管，不提取或复制凭据。
- `--tools "" --strict-mcp-config --disable-slash-commands --safe-mode --setting-sources "" --settings '{"disableAllHooks":true}'` 限制工具及用户自定义；会话使用 `--no-session-persistence`。
- 私有 system 经独立匿名管道交给 `--system-prompt-file /dev/fd/N`，user 仅走 stdin，保留指令层级；两者不进入 argv、环境或普通文件。管道并发写入，超时/取消清理整个进程组。
- init 必须没有工具/MCP；assistant 必须绑定同一 init 的 session/model、assistant role 和合法文本，result 须成功且与 assistant 文本相同。未知、尾随、工具或不完整事件拒绝。
- 设置 `CLAUDE_CODE_MAX_RETRIES=0`、`CLAUDE_CODE_RETRY_WATCHDOG=0`、`CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK=1`、`CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES=0`。OmniDesk 不重试、不修复重发、不转付费 profile；实际 CLI 网络行为尚未验收。
- 仅 POSIX、文本输入；SSE 是完整结果后的缓冲输出（`native=false`）。账本 USD 估算为 0，仍消费订阅额度；输入 token 包括缓存读取/创建，不代表无限额度或供方硬输出上限。

```bash
PYTHONPATH=. python -m pytest -q -W error tests/models/test_claude_code_cli_provider.py tests/test_codex_cli_gateway.py
```

## 启用前的阻塞项

1. 本次未找到可验收的已安装 CLI，未登录、安装或调用真实 Claude 模型。需本人提供可信绝对 `claude_code_cli_path` 或在 PATH 安装官方 CLI，再核固定版本、个人订阅认证、别名实际解析/CLI内部模型fallback和真实事件结构；当前返回/记账的model为配置别名，不是已验实际模型身份。
2. 官方只保证 `--system-prompt-file` 读取文件；`/dev/fd/N` 与该版本实际运行时的兼容性仍需验证。假 CLI 的成功、长管道和清理测试不能替代这项证据。
3. `--safe-mode` 和普通 `disableAllHooks` **不能保证阻止 managed hooks**。企业/受管理宿主当前不支持启用；不要绕过 managed policy。本次没有实现完整 managed policy 检测，也不声明宿主零读取/零执行。个人宿主仍须用合成 hooks 验证无调用、无额外设置/凭据注入后才能启用。
4. 真实 Gateway 验收须覆盖匿名/operator 拒绝、owner 文本与 SSE、幂等重放、持久账本及失败不回退，并绑定精确源代码。Linux/Windows、云端和生产均未验收。

## 回滚

保持或恢复 `enabled: false`，移除指向该 profile 的路由并恢复先前配置/部署；保留账本与审计。代码发布回滚通过新的 `ai/*` PR 撤销本次提交，不直接修改 main。

## 官方契约来源

- [CLI 参数与认证枚举](https://code.claude.com/docs/en/cli-reference)
- [Hooks](https://code.claude.com/docs/en/hooks)、[设置与 managed 优先级](https://code.claude.com/docs/en/settings)
- [重试行为](https://code.claude.com/docs/en/errors#tune-retry-behavior)、[环境变量](https://code.claude.com/docs/en/env-vars)
- [Managed policy 来源](https://code.claude.com/docs/en/managed-settings#where-each-mechanism-stores-the-policy)、[远端 managed policy](https://code.claude.com/docs/en/server-managed-settings)
