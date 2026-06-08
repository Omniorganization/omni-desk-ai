# Omni-deskAi
Omni-deskAi

Omni-deskAi 是一个本地优先（local-first）的多渠道 AI 助理运行时工程。它的目标是把 Computer-use、跨软件自动化、多渠道消息接入、层级规划、经验检索、skills/tools 扩展、权限验证 合并到一个可审计、可扩展、可长期运行的 AI 助理框架中。

当前版本是工程骨架与运行时基础版本，适合继续扩展为个人 AI 助理、企业客服助理、运营自动化助理、桌面办公自动化 agent 或多渠道消息中台。

⸻

核心能力

1. Computer-use：看屏幕、点按钮、跨软件操作

Omni-deskAi 内置 computer tool，支持：

* 屏幕截图
* 鼠标移动
* 鼠标点击
* 键盘输入
* 快捷键
* 通过可见桌面 UI 操作 WhatsApp、Telegram、WeChat、Chrome、Safari、Edge 等软件

所有 Computer-use 动作在执行前都会进入权限验证流程，避免 AI 在无人确认的情况下点击、输入、发送消息或操作系统 UI。

⸻

2. 多渠道消息接入

项目内置多渠道 adapter 骨架：

渠道	接入方式	文件
Telegram	Telegram Bot API	omnidesk_agent/channels/telegram.py
WhatsApp	WhatsApp Business Cloud API	omnidesk_agent/channels/whatsapp_cloud.py
WeChat	微信公众号 / 服务号消息接口	omnidesk_agent/channels/wechat_official.py
Facebook / Instagram	Meta Graph API	omnidesk_agent/channels/meta_graph.py
桌面软件	Visible UI Bridge	omnidesk_agent/channels/ui_bridge.py

合规边界：

* WhatsApp、Facebook、Instagram 默认通过官方 API 接入。
* WeChat 默认通过公众号 / 服务号接口接入。
* 个人账号类操作只能通过用户已登录、屏幕可见、每一步确认的 UI Bridge 执行。
* 项目不包含绕过登录、盗取 Cookie、规避平台风控、隐藏控制个人账号、批量骚扰或违反平台条款的代码。

⸻

3. 层级规划 + 经验检索

Omni-deskAi 内置基础 planner 和 SQLite 经验库：

* HierarchicalPlanner：把用户任务拆解成可执行步骤。
* ExperienceStore：保存历史经验、操作偏好、业务规则和复用知识。
* 当前默认 planner 可用 rule 模式运行，后续可替换成 LLM JSON planner。

经验相关命令：

omnidesk remember "回复客户报价前先检查库存表" --tags sales,inventory
omnidesk search "报价 库存"

⸻

4. Skills / Tools 扩展机制

项目支持加载 Codex / OpenClaw 风格的技能说明文件：

~/.omnidesk/skills/<skill-name>/SKILL.md

Skills 用来描述工作流、操作规范、业务知识和工具使用方法。Tools 则是真正可执行的动作，例如：

* computer
* shell
* files
* channels
* 后续自定义插件工具

⸻

5. 每一步操作前权限验证

所有会产生副作用的动作都会生成 ActionProposal，然后进入 PermissionManager.verify()。

权限验证会检查：

* 动作来源渠道
* 发起人身份
* 工具类型
* 动作风险等级
* 是否涉及 shell、文件写入、消息发送、鼠标键盘、系统 UI
* 当前是否处于无 TTY 后台模式
* 是否命中 allow / deny / ask 策略

默认策略：

permissions:
  default_mode: ask
  no_tty_mode: deny
  always_ask_tools: [computer, shell, channels, files]

这意味着：

* 本地前台运行时，高风险动作会询问用户。
* 后台无人值守时，高风险动作默认拒绝。
* shell、文件写入、鼠标键盘、消息发送默认都需要确认。

⸻

项目结构

Omni-desk-AI/
  pyproject.toml
  README.md
  GITHUB_PUSH.md
  examples/
    config.yaml
  omnidesk_agent/
    __init__.py
    cli.py
    config.py
    daemon.py
    server.py
    core/
      llm.py
      models.py
      orchestrator.py
      planner.py
    memory/
      experience.py
    security/
      permissions.py
    tools/
      base.py
      registry.py
      computer.py
      shell.py
      files.py
    channels/
      base.py
      telegram.py
      whatsapp_cloud.py
      wechat_official.py
      meta_graph.py
      ui_bridge.py
    skills/
      registry.py
  deploy/
    systemd/
      omnidesk-agent.service
    launchd/
      com.omnidesk.agent.plist

⸻

快速开始

git clone https://github.com/yinyufan0813-cmyk/Omni-deskAi.git
cd Omni-deskAi
python -m venv .venv
source .venv/bin/activate
pip install -e .
mkdir -p ~/.omnidesk
cp examples/config.yaml ~/.omnidesk/config.yaml
omnidesk doctor --config ~/.omnidesk/config.yaml
omnidesk serve --config ~/.omnidesk/config.yaml

Windows PowerShell：

python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .

⸻

常用命令

# 执行一条本地任务
omnidesk run "看一下当前屏幕，并告诉我现在打开了什么软件"
# 测试 shell 工具
omnidesk run "shell: ls -la"
# 添加经验
omnidesk remember "处理客户退款前先核对订单状态、支付记录和物流状态" --tags customer-service,refund
# 搜索经验
omnidesk search "退款 物流"
# 启动 Webhook Gateway
omnidesk serve --host 127.0.0.1 --port 18789

⸻

安全设计

Omni-deskAi 的安全原则：

* 本地优先：敏感数据优先保存在本机。
* 最小权限：工具能力按配置启用，不默认开放全部操作。
* 每步审批：高风险动作必须经过权限验证。
* 可审计：操作会写入 audit log。
* 工作区隔离：文件工具默认限制在 workspace 内。
* shell 限时：shell 命令有最大执行时间限制。
* 渠道白名单：外部消息来源需要 allowed sender 过滤。
* 无人值守保护：后台无 TTY 时默认拒绝高风险动作。

⸻

当前版本限制

当前版本是工程骨架，不是完整商业化产品。主要限制：

1. Planner 默认是 rule 模式，需要接入 LLM 后才能实现更强的复杂任务规划。
2. 远程审批面板尚未实现，后台模式下高风险动作默认拒绝。
3. 渠道 adapter 已有骨架，真实生产使用需要补充 token、webhook、权限、错误重试和日志。
4. Computer-use 基于屏幕和坐标，复杂 UI 自动化建议接入更强的视觉 grounding model。
5. 没有实现非官方个人账号接口，也不会绕过平台登录或限制。

⸻

License

当前仓库尚未指定开源协议。正式公开或商用前，建议补充 LICENSE 文件，例如 MIT、Apache-2.0 或私有商业授权。
