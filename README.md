# Omni-deskAi

Omni-deskAi 是一个本地优先（local-first）的多渠道 AI 助理运行时工程，目标是把 Computer-use、多 App 消息接入、跨软件 UI 操作、层级规划、经验检索、Skills、Tools、Plugins、自我升级、多模型路由、Token 防浪费和每一步权限验证整合成一个可审计、可扩展、可长期运行的 AI 助理框架。

> 当前版本是工程化运行时基础版本，不是完整商业化成品。它已经具备多渠道接入口骨架、桌面可见 UI Bridge、动态插件加载、Skills 参与规划、多模型 Provider 路由、自我升级 Level 1–3、TokenBudget 统一管控等核心能力。

---

## 近期重大更新总览

### 1. 自我升级 Self-upgrade Level 1–3

新增目录：

```text
omnidesk_agent/self_upgrade/
  __init__.py
  models.py
  analyzer.py
  planner.py
  patcher.py
  tester.py
  upgrader.py
```

已支持：

```text
Level 1：生成升级报告，不改代码
Level 2：生成升级计划和补丁说明，不自动提交
Level 3：人工批准后创建 ai/* 分支、运行测试、提交升级方案、可选 push
```

明确未启用：

```text
Level 4：自动合并 PR
Level 4：自动重启服务
Level 4：自动 push main
Level 4：自动 force push
Level 4：自动绕过人工审批
```

命令示例：

```bash
omnidesk upgrade-report --output UPGRADE_REPORT.md
omnidesk propose-upgrade "增强 Telegram 消息重试机制"
omnidesk upgrade "增强渠道消息重试机制" --approved --push --create-pr
```

---

### 2. Token 防浪费机制

新增：

```text
omnidesk_agent/core/token_budget.py
omnidesk_agent/core/execution_strategy.py
```

核心流程：

```text
先判断是否有必要执行
再判断是否会浪费 token
再检查缓存
再截断不必要上下文
再调用模型
再记录 usage
```

当前设计：

```text
TokenBudgetManager 不再是单任务硬预算
已验证必须使用的 token 可以突破预算阈值
超过阈值时记录 budget_overridden
未验证必要性的超大请求会被阻止
```

也就是说，它不是为了省 token 而影响必要任务，而是阻止无目的、重复、未验证的大模型调用。

---

### 3. Computer-use 结果导向执行

`computer` 工具已升级为“先目标，后执行”。

以下动作必须携带 `expected_result`：

```text
computer.screenshot
computer.click
computer.move
computer.type_text
computer.hotkey
```

示例：

```json
{
  "tool": "computer",
  "action": "screenshot",
  "args": {
    "expected_result": "确认 WhatsApp 是否打开客户聊天窗口",
    "max_width": 960,
    "skip_if_unchanged": true,
    "skip_if_too_soon": true
  }
}
```

截图优化：

```text
默认缩放截图宽度
相同截图不重复分析
短时间内连续截图会跳过
截图必须声明目标
返回 screenshot hash
```

---

### 4. 多 App / 多渠道接入口覆盖

当前已覆盖以下接入口层：

```text
WhatsApp
WhatsApp Business
WeChat / 微信
DingTalk / 钉钉
Lark
Feishu / 飞书
Xiaohongshu / 小红书
LINE
X / Twitter
Telegram
Facebook
Instagram
Google Chrome
Gmail
```

#### 官方 API / Webhook 方式

```text
Telegram                Telegram Bot API
WhatsApp Business       WhatsApp Cloud API
WeChat Official         微信公众号 / 服务号
DingTalk                钉钉机器人 / Open Platform
Lark                    Lark Open Platform
Feishu                  飞书 Open Platform
LINE                    LINE Messaging API
X                       X API / webhook CRC
Facebook / Instagram    Meta Graph API
Gmail                   Gmail API skeleton
Google Chrome           Chrome DevTools Protocol
```

#### 可见 UI Bridge 方式

用于个人账号或没有合规开放 API 的应用：

```text
WhatsApp 个人版
微信个人号
小红书
个人 Facebook / Instagram 场景
Chrome 内网页操作
Gmail 网页端
其他已登录桌面软件
```

UI Bridge 原则：

```text
不绕过登录
不读取 Cookie
不隐藏控制账号
不做逆向协议
不自动规避平台限制
所有点击、输入、发送前都要权限验证
```

---

### 5. 新增 Channel Adapters

新增文件：

```text
omnidesk_agent/channels/dingtalk.py
omnidesk_agent/channels/lark_feishu.py
omnidesk_agent/channels/line.py
omnidesk_agent/channels/x_channel.py
omnidesk_agent/channels/gmail.py
```

原有文件：

```text
omnidesk_agent/channels/telegram.py
omnidesk_agent/channels/whatsapp_cloud.py
omnidesk_agent/channels/wechat_official.py
omnidesk_agent/channels/meta_graph.py
omnidesk_agent/channels/ui_bridge.py
```

---

### 6. 新增统一 Channel Send Tool

新增：

```text
omnidesk_agent/tools/channel_send.py
```

统一外部消息发送入口：

```text
channels.send_text
channels.send_email
```

所有发送动作统一经过：

```text
PermissionManager.verify()
```

避免 AI 在未确认情况下向外部平台发送消息。

---

### 7. 新增 UI Bridge Tool

新增：

```text
omnidesk_agent/tools/ui_bridge_tool.py
```

支持：

```text
ui_bridge.observe
ui_bridge.click
ui_bridge.type_visible_reply
ui_bridge.press_send
```

所有 UI Bridge 操作都会调用 Computer-use，并自动携带 `expected_result`。

---

### 8. 新增 Browser Tool

新增：

```text
omnidesk_agent/tools/browser.py
```

用于连接 Google Chrome DevTools Protocol。

Chrome 启动示例：

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222
```

支持：

```text
browser.list_tabs
browser.new_tab
```

---

### 9. 新增 Gmail Tool

新增：

```text
omnidesk_agent/tools/gmail_tool.py
```

当前支持 Gmail API payload skeleton：

```text
gmail.configured
gmail.build_raw_email
```

Gmail 真实收发需要用户配置 Google OAuth credentials。

---

### 10. Skills 已参与 Planner

原先 Skills 只是加载，但不会真正影响规划。

现在升级为：

```text
SkillRegistry.load()
SkillRegistry.match(query)
SkillRegistry.prompt_block(query)
HierarchicalPlanner.plan()
_rule_plan(..., skills_context)
```

Skills 路径：

```text
~/.omnidesk/skills/<skill-name>/SKILL.md
```

示例：

```text
examples/skills/channel-routing/SKILL.md
```

---

### 11. 动态 Plugins 加载机制

新增：

```text
omnidesk_agent/plugins/
  __init__.py
  registry.py
```

插件目录结构：

```text
~/.omnidesk/plugins/<plugin-name>/
  plugin.yaml
  plugin.py
```

`plugin.yaml` 示例：

```yaml
name: echo
enabled: true
trusted: true
description: Example trusted plugin
```

`plugin.py` 示例：

```python
class EchoTool:
    name = "echo"

    async def call(self, action, args, ctx):
        ...

def register(tool_registry, app_config=None):
    tool_registry.register(EchoTool())
    return ["echo"]
```

默认安全策略：

```yaml
plugins:
  enabled: true
  trusted_only: true
  allowlist: []
```

即默认只加载 `trusted: true` 的插件，避免第三方代码静默执行。

---

### 12. 验证系统

新增：

```text
omnidesk_agent/validation/
  __init__.py
  connectors.py
  extensions.py
  models.py
```

验证命令：

```bash
omnidesk validate-connectors --config examples/config.yaml
omnidesk validate-extensions --config examples/config.yaml
omnidesk validate-models --config examples/config.yaml
```

如果 `omnidesk` 命令还未安装：

```bash
python3 -m omnidesk_agent.cli validate-connectors --config examples/config.yaml
python3 -m omnidesk_agent.cli validate-extensions --config examples/config.yaml
python3 -m omnidesk_agent.cli validate-models --config examples/config.yaml
```

---

### 13. 多模型 ModelRouter

新增：

```text
omnidesk_agent/models/
  __init__.py
  base.py
  providers.py
  router.py
```

模型调用已经升级为：

```text
ModelRequest
  ↓
ModelRouter 按任务类型选择模型
  ↓
TokenBudgetManager 统一判断 / 缓存 / 截断 / override
  ↓
具体 Provider 调用
  ↓
记录 usage
```

关键变化：

```text
TokenBudgetManager 已上移到 ModelRouter 层
不再只挂在 OpenAIChatLLM
所有 Provider 调用统一经过 token guardrail
```

---

## 支持的模型 Provider

当前支持的 provider alias：

```text
openai
openai_responses
openai_chat
openai_compatible
azure_openai
anthropic
claude
gemini
google
ollama
deepseek
qwen
dashscope
groq
mistral
xai
openrouter
together
fireworks
perplexity
moonshot
kimi
zhipu
cohere
baidu_qianfan
bedrock
```

| Provider | 说明 |
|---|---|
| `openai` / `openai_responses` | OpenAI Responses API |
| `openai_compatible` / `openai_chat` | 通用 OpenAI-compatible Chat Completions |
| `azure_openai` | Azure OpenAI deployment |
| `anthropic` / `claude` | Anthropic Claude Messages API |
| `gemini` / `google` | Google Gemini generateContent |
| `ollama` | 本地 Ollama `/api/chat` |
| `deepseek` | DeepSeek OpenAI-compatible |
| `qwen` / `dashscope` | 通义千问 / 阿里云 DashScope compatible |
| `groq` | Groq OpenAI-compatible |
| `mistral` | Mistral API |
| `xai` | xAI Grok compatible |
| `openrouter` | OpenRouter |
| `together` | Together AI |
| `fireworks` | Fireworks AI |
| `perplexity` | Perplexity Sonar |
| `moonshot` / `kimi` | Moonshot / Kimi |
| `zhipu` | 智谱 GLM |
| `cohere` | Cohere v2 chat |
| `baidu_qianfan` | 百度千帆 |
| `bedrock` | AWS Bedrock Claude-style invoke_model |

---

## 任务级模型路由

配置示例：

```yaml
models:
  default: fast

  routing:
    planner: planner
    tool_plan: planner
    chat: fast
    code: code
    upgrade: code
    vision: vision
    private: local
    summarize: fast

  profiles:
    fast:
      enabled: true
      provider: openai
      model: gpt-5.1-mini
      api_key_env: OPENAI_API_KEY
      max_output_tokens: 800

    planner:
      enabled: true
      provider: openai
      model: gpt-5.1
      api_key_env: OPENAI_API_KEY
      max_output_tokens: 1600

    code:
      enabled: true
      provider: openai
      model: gpt-5.1
      api_key_env: OPENAI_API_KEY
      max_output_tokens: 4000

    vision:
      enabled: true
      provider: openai
      model: gpt-5.1
      api_key_env: OPENAI_API_KEY
      max_output_tokens: 1600

    local:
      enabled: true
      provider: ollama
      model: qwen2.5-coder:7b
      api_key_env: null
      base_url: http://127.0.0.1:11434
```

推荐分工：

| 任务 | 路由 |
|---|---|
| 层级规划 | `planner` |
| 工具规划 | `planner` |
| 普通聊天 | `fast` |
| 代码修改 | `code` |
| 自我升级 | `upgrade -> code` |
| 截图理解 | `vision` |
| 隐私任务 | `private -> local` |
| 总结压缩 | `summarize -> fast` |

---

## 快速开始

### 1. Clone 仓库

```bash
git clone https://github.com/yinyufan0813-cmyk/Omni-deskAi.git
cd Omni-deskAi
```

### 2. 创建虚拟环境

macOS / Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 3. 安装

```bash
pip install -e .
```

### 4. 初始化配置

```bash
mkdir -p ~/.omnidesk
cp examples/config.yaml ~/.omnidesk/config.yaml
```

### 5. 检查运行状态

```bash
omnidesk doctor --config ~/.omnidesk/config.yaml
```

---

## 常用命令

### 启动 Gateway

```bash
omnidesk serve --config ~/.omnidesk/config.yaml
```

### 本地执行任务

```bash
omnidesk run "看一下当前屏幕，并告诉我现在打开了什么软件"
```

### 添加经验

```bash
omnidesk remember "回复客户报价前先检查库存表" --tags sales,inventory
```

### 搜索经验

```bash
omnidesk search "报价 库存"
```

### 生成升级报告

```bash
omnidesk upgrade-report --output UPGRADE_REPORT.md
```

### 生成升级方案

```bash
omnidesk propose-upgrade "增强 Telegram 消息重试机制"
```

### 验证接入口

```bash
omnidesk validate-connectors --config examples/config.yaml
```

### 验证 Skills / Plugins

```bash
omnidesk validate-extensions --config examples/config.yaml
```

### 验证多模型路由

```bash
omnidesk validate-models --config examples/config.yaml
```

---

## Webhook 路由

当前 Gateway 提供：

```text
GET  /health
POST /agent/run

POST /webhooks/telegram

GET  /webhooks/whatsapp
POST /webhooks/whatsapp

GET  /webhooks/meta
POST /webhooks/meta

GET  /webhooks/wechat
POST /webhooks/wechat

POST /webhooks/dingtalk
POST /webhooks/lark
POST /webhooks/feishu
POST /webhooks/line

GET  /webhooks/x
POST /webhooks/x

GET  /validate/connectors
GET  /validate/extensions
```

---

## 环境变量

按需配置：

```bash
export OPENAI_API_KEY="..."
export ANTHROPIC_API_KEY="..."
export GEMINI_API_KEY="..."
export DEEPSEEK_API_KEY="..."
export DASHSCOPE_API_KEY="..."

export TELEGRAM_BOT_TOKEN="..."

export WHATSAPP_CLOUD_TOKEN="..."
export WHATSAPP_VERIFY_TOKEN="..."

export WECHAT_APP_ID="..."
export WECHAT_APP_SECRET="..."
export WECHAT_TOKEN="..."

export META_PAGE_ACCESS_TOKEN="..."
export META_VERIFY_TOKEN="..."

export DINGTALK_ROBOT_TOKEN="..."
export DINGTALK_ROBOT_SECRET="..."

export LARK_APP_ID="..."
export LARK_APP_SECRET="..."
export LARK_VERIFICATION_TOKEN="..."

export FEISHU_APP_ID="..."
export FEISHU_APP_SECRET="..."
export FEISHU_VERIFICATION_TOKEN="..."

export LINE_CHANNEL_ACCESS_TOKEN="..."
export LINE_CHANNEL_SECRET="..."

export X_BEARER_TOKEN="..."
export X_WEBHOOK_CRC_TOKEN="..."
```

---

## 项目结构

```text
Omni-deskAi/
  README.md
  MODEL_ROUTER.md
  pyproject.toml
  examples/
    config.yaml
    plugins/
      echo/
        plugin.yaml
        plugin.py
    skills/
      channel-routing/
        SKILL.md

  omnidesk_agent/
    cli.py
    config.py
    daemon.py
    server.py

    core/
      llm.py
      models.py
      orchestrator.py
      planner.py
      token_budget.py
      execution_strategy.py

    models/
      __init__.py
      base.py
      providers.py
      router.py

    channels/
      telegram.py
      whatsapp_cloud.py
      wechat_official.py
      meta_graph.py
      dingtalk.py
      lark_feishu.py
      line.py
      x_channel.py
      gmail.py
      ui_bridge.py

    tools/
      computer.py
      shell.py
      files.py
      git_tool.py
      test_tool.py
      channel_send.py
      ui_bridge_tool.py
      browser.py
      gmail_tool.py
      registry.py

    skills/
      registry.py

    plugins/
      registry.py

    self_upgrade/
      models.py
      analyzer.py
      planner.py
      patcher.py
      tester.py
      upgrader.py

    validation/
      connectors.py
      extensions.py
      models.py

    security/
      permissions.py

    memory/
      experience.py

  deploy/
    systemd/
      omnidesk-agent.service
    launchd/
      com.omnidesk.agent.plist
```

---

## 安全设计

Omni-deskAi 的安全原则：

```text
本地优先
最小权限
每步审批
默认高风险动作 ask
后台无 TTY 默认 deny
shell 命令限时
危险 shell pattern 拦截
外部消息发送必须确认
Computer-use 必须声明 expected_result
个人账号只走可见 UI Bridge
不绕过登录
不读取 Cookie
不隐藏控制账号
不自动合并 PR
不自动重启服务
```

---

## 权限验证

所有副作用动作都应该经过：

```text
PermissionManager.verify()
```

高风险工具包括：

```text
computer
shell
channels
files
ui_bridge
browser
gmail
git
```

默认配置：

```yaml
permissions:
  default_mode: ask
  no_tty_mode: deny
  always_ask_tools:
    - computer
    - shell
    - channels
    - files
    - ui_bridge
    - browser
    - gmail
```

---

## 当前限制

当前项目仍然是工程骨架和运行时基础版本，存在以下限制：

1. 很多平台虽然已具备 adapter skeleton，但真实生产接入仍需要平台 API 权限、OAuth app、Webhook 配置和审核。
2. Gmail API 当前主要是 payload / credentials skeleton，完整 OAuth 授权流程需要继续补齐。
3. Chrome DevTools 需要用户手动开启 remote debugging port。
4. 个人账号类 App 不提供非官方逆向接入，只能通过可见 UI Bridge。
5. 自我升级只做到 Level 1–3，不自动合并、不自动重启。
6. Vision grounding 仍需接入真实视觉模型和坐标定位模型。
7. Plugins 默认只加载 trusted plugin，但仍需用户自行审查第三方插件代码。
8. Model Provider 已覆盖主流接口类型，但每个模型真实调用仍依赖 API key、region、model name、base_url 和账号权限。

---

## 推荐下一步

建议后续继续完善：

```text
1. 移除仓库中误提交的运行时数据库文件
2. 完善 Gmail OAuth flow
3. 完善 Chrome DevTools 页面控制
4. 新增 screenshot 存文件而非默认返回 base64
5. 新增 Vision grounding provider
6. 新增 PR 自动创建但不自动合并
7. 新增远程审批 UI
8. 新增更多单元测试
9. 新增 provider live connectivity test
10. 新增 channel webhook signature test
```

---

## License

当前仓库尚未指定开源协议。正式公开或商用前，建议补充 `LICENSE` 文件，例如 MIT、Apache-2.0 或私有商业授权。
